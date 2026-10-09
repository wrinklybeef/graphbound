"""A small synchronous Microsoft Graph client that stays on one host."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Self
from urllib.parse import unquote, urlsplit, urlunsplit

import requests

from graphbound.errors import (
    INVALID_RESPONSE_CODE,
    REQUEST_FAILED_CODE,
    UNEXPECTED_REDIRECT_CODE,
    GraphError,
    GraphErrorDetails,
    GraphRetryExhausted,
    UnsafeGraphUrl,
)
from graphbound.events import RequestFailure, RetryScheduled
from graphbound.retry import (
    TRANSIENT_READ_STATUSES,
    ReadBudget,
    ReadRetryPolicy,
    Sleep,
    current_read_budget,
    retry_after_header,
)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0/"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_BATCH_REQUESTS = 20

_ABSOLUTE_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_CONTROL_OR_BACKSLASH = re.compile(r"[\x00-\x1f\x7f\\]")
_SAFE_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,99}")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_THROTTLE_POLICY = ReadRetryPolicy()

_Send = Callable[[float], requests.Response]


def _split_https(url: str) -> tuple[str, str, str]:
    """Return ``(host, path, query)`` for a plain ``https://host[:443]/...`` URL, or refuse it."""
    if _CONTROL_OR_BACKSLASH.search(url):
        raise UnsafeGraphUrl("Graph URLs cannot contain control characters or backslashes.")
    if "#" in url:
        # A fragment is never sent, so anything after it silently drops off the request.
        raise UnsafeGraphUrl("Graph URLs cannot contain a fragment.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeGraphUrl("Graph URLs must be valid HTTPS URLs with the default port.") from None
    host = parts.hostname
    if parts.scheme != "https" or not host:
        raise UnsafeGraphUrl("Graph URLs must be absolute https URLs.")
    if "@" in parts.netloc:
        raise UnsafeGraphUrl("Graph URLs cannot contain credentials.")
    if port not in (None, 443):
        raise UnsafeGraphUrl("Graph URLs must use the default HTTPS port.")
    return host, parts.path, parts.query


class GraphClient:
    """Send JSON requests to Microsoft Graph with one bearer token.

    The token is only ever sent to the host of ``base_url``. Relative paths are
    resolved against ``base_url``; an absolute URL (an ``@odata.nextLink``, or a
    ``/beta`` endpoint) is accepted only when it is on that same host.

    Retrying:

    * With a ``read_policy``, ``GET`` requests retry transient statuses and
      transport errors within the policy and any active
      :func:`~graphbound.retry.read_budget`.
    * Everything else is sent once, plus one more try after a ``429`` whose
      ``Retry-After`` the client can honor. A ``429`` means Graph did not
      process the request, so that replay is safe for writes too. Pass
      ``retry_throttling=False`` when the caller owns throttling.
    * Writes are never replayed after a timeout or a 5xx, because the first
      attempt may have been applied.

    ``on_failure`` and ``on_retry`` receive events that hold no URL, body, or
    provider message.
    """

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = GRAPH_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
        sleep: Sleep = time.sleep,
        read_policy: ReadRetryPolicy | None = None,
        retry_throttling: bool = True,
        on_failure: Callable[[RequestFailure], None] | None = None,
        on_retry: Callable[[RetryScheduled], None] | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("An HTTP timeout must be finite and positive.")
        host, base_path, base_query = _split_https(base_url)
        if base_query:
            raise UnsafeGraphUrl("The Graph base URL cannot contain a query string.")
        self._host = host
        self._base_url = f"https://{host}{base_path.rstrip('/')}/"
        self._timeout = timeout
        self._owns_session = session is None
        self._session = session or requests.Session()
        self._sleep = sleep
        self._read_policy = read_policy
        self._retry_throttling = retry_throttling
        self._on_failure = on_failure
        self._on_retry = on_retry
        self._authorization = f"Bearer {access_token}"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP session if this client created it."""
        if self._owns_session:
            self._session.close()

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request("GET", path, params=params, headers=headers)

    def post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", path, json=json, headers=headers)

    def patch(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request("PATCH", path, json=json, headers=headers)

    def delete(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request("DELETE", path, headers=headers)

    def read_batch_once(
        self,
        subrequests: Sequence[Mapping[str, Any]],
        *,
        path: str = "$batch",
    ) -> dict[str, Any]:
        """Send one JSON batch of ``GET`` subrequests, exactly once.

        The batch itself is never retried here. Each subresponse carries its
        own status and ``Retry-After``, so the caller decides which
        subrequests to send again. Batches containing writes are refused.
        """
        items = list(subrequests)
        if not 1 <= len(items) <= MAX_BATCH_REQUESTS:
            raise ValueError(f"A Graph batch holds 1 to {MAX_BATCH_REQUESTS} requests.")
        if any(not isinstance(item, Mapping) or item.get("method") != "GET" for item in items):
            raise ValueError("Only read-only Graph batches are supported.")
        if not path.endswith("$batch"):
            raise ValueError("A batch must be posted to a $batch endpoint.")
        return self._request("POST", path, json={"requests": items}, single_attempt=True)

    def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a collection, following ``@odata.nextLink``."""
        for page in self.paginate_pages(path, params=params, headers=headers):
            yield from page

    def paginate_pages(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield a collection one page at a time.

        A page whose ``value`` is missing or malformed raises instead of ending
        the iteration, so a broken response cannot pass for a short list.
        """
        next_url: str | None = path
        next_params = params
        seen: set[str | None] = set()
        while next_url is not None:
            # Compare the actual encoded URL, including first-page parameters.
            url = requests.Request("GET", self._resolve(next_url), params=next_params).prepare().url
            if url in seen:
                raise _invalid_response(200, "Microsoft Graph returned a pagination cycle.")
            seen.add(url)
            page = self.get(next_url, params=next_params, headers=headers)
            values = page.get("value")
            if not isinstance(values, list):
                raise _invalid_response(200, "Microsoft Graph response 'value' was not a list.")
            if any(not isinstance(item, dict) for item in values):
                raise _invalid_response(200, "Microsoft Graph response contained an invalid item.")
            yield values
            if "@odata.nextLink" not in page:
                return
            next_link = page["@odata.nextLink"]
            if (
                not isinstance(next_link, str)
                or not _ABSOLUTE_URL.match(next_link)
                or any(char.isspace() for char in next_link)
            ):
                raise _invalid_response(
                    200, "Microsoft Graph response '@odata.nextLink' was invalid."
                )
            next_url = next_link
            # The nextLink already carries the query; sending params again would duplicate it.
            next_params = None

    def _resolve(self, path: str) -> str:
        """Turn a path or absolute URL into the exact URL to send, or refuse it."""
        candidate = path if _ABSOLUTE_URL.match(path) else self._base_url + path.lstrip("/")
        host, url_path, query = _split_https(candidate)
        if host != self._host:
            raise UnsafeGraphUrl(f"Graph requests must stay on https://{self._host}.")
        # Decoded first: the HTTP library turns %2e back into a dot before sending.
        if any(unquote(segment) in {".", ".."} for segment in url_path.split("/")):
            raise UnsafeGraphUrl("Graph paths cannot contain dot segments.")
        # Rebuilt from the validated parts so the HTTP library cannot read it differently.
        return urlunsplit(("https", host, url_path, query, ""))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        single_attempt: bool = False,
    ) -> dict[str, Any]:
        url = self._resolve(path)
        request_headers = {
            "Accept": "application/json",
            **(headers or {}),
            "Authorization": self._authorization,
        }

        last_response: requests.Response | None = None
        attempts = 0

        def send(timeout: float) -> requests.Response:
            nonlocal last_response, attempts
            last_response = None
            attempts += 1
            last_response = self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=request_headers,
                timeout=timeout,
                allow_redirects=False,
            )
            return last_response

        policy = self._read_policy if method == "GET" else None
        try:
            if policy is not None:
                response = self._send_read(method, send, policy)
            else:
                retry_once = self._retry_throttling and not single_attempt
                response = self._send_write_safe(method, send, attempts=2 if retry_once else 1)
        except GraphRetryExhausted as exc:
            exc.attempts = attempts
            if last_response is None:
                raise
            failure = _error_from(last_response)
            raise GraphRetryExhausted(
                last_response.status_code,
                retry_after=failure.retry_after,
                reason=exc.reason,
                request_id=failure.details.request_id,
                client_request_id=failure.details.client_request_id,
                attempts=attempts,
            ) from exc
        return self._decode(response)

    def _send_read(self, method: str, send: _Send, policy: ReadRetryPolicy) -> requests.Response:
        """Retry an idempotent read inside the policy and the active budget."""
        budget = current_read_budget() or ReadBudget(policy.max_elapsed, sleep=self._sleep)
        started = budget.clock()
        attempt = 0
        while True:
            attempt += 1
            policy_remaining = policy.max_elapsed - (budget.clock() - started)
            timeout = min(self._timeout, budget.remaining(), policy_remaining)
            if timeout <= 0:
                raise GraphRetryExhausted()
            try:
                response = send(timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._notify_failure(method, attempt, error=exc)
                policy.wait(attempt, started, budget, on_retry=self._on_retry)
                continue
            if not response.ok:
                self._notify_failure(method, attempt, response=response)
            # An answer that arrives after the deadline is not used.
            budget.remaining()
            if budget.clock() - started >= policy.max_elapsed:
                raise GraphRetryExhausted()
            if response.status_code not in TRANSIENT_READ_STATUSES:
                return response
            policy.wait(
                attempt,
                started,
                budget,
                status=response.status_code,
                headers=response.headers,
                on_retry=self._on_retry,
            )

    def _send_write_safe(self, method: str, send: _Send, *, attempts: int) -> requests.Response:
        """Send once; replay only a 429, which Graph guarantees it did not process."""
        budget = current_read_budget()
        attempt = 0
        while True:
            attempt += 1
            timeout = min(self._timeout, budget.remaining()) if budget else self._timeout
            try:
                response = send(timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._notify_failure(method, attempt, error=exc)
                raise
            if not response.ok:
                self._notify_failure(method, attempt, response=response)
            if budget is not None:
                budget.remaining()
            if response.status_code != 429 or attempt >= attempts:
                return response
            try:
                delay = _THROTTLE_POLICY.delay(response.headers, attempt, jitter=lambda: 0.0)
            except GraphRetryExhausted:
                # The cooldown is longer than this client waits. Hand the caller the real 429.
                return response
            if self._on_retry is not None:
                self._on_retry(
                    RetryScheduled(attempt=attempt, delay_seconds=delay, status_code=429)
                )
            if budget is not None:
                budget.pause(delay, response.status_code)
            else:
                self._sleep(delay)

    def _notify_failure(
        self,
        method: str,
        attempt: int,
        *,
        response: requests.Response | None = None,
        error: Exception | None = None,
    ) -> None:
        if self._on_failure is None:
            return
        self._on_failure(
            RequestFailure(
                method=method,
                attempt=attempt,
                status_code=response.status_code if response is not None else None,
                error_code=_safe_error_code(response) if response is not None else None,
                exception_type=type(error).__name__ if error is not None else None,
            )
        )

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any]:
        request_id = response.headers.get("request-id")
        if response.status_code in _REDIRECT_STATUSES:
            raise GraphError(
                GraphErrorDetails(
                    response.status_code,
                    UNEXPECTED_REDIRECT_CODE,
                    "Microsoft Graph answered with a redirect, which this client does not follow.",
                    request_id,
                )
            )
        if not response.ok:
            raise _error_from(response)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise _invalid_response(
                response.status_code, "Microsoft Graph returned a non-JSON response.", request_id
            ) from exc
        if not isinstance(body, dict):
            raise _invalid_response(
                response.status_code,
                "Microsoft Graph returned an unexpected JSON response.",
                request_id,
            )
        return body


def _invalid_response(status_code: int, message: str, request_id: str | None = None) -> GraphError:
    return GraphError(GraphErrorDetails(status_code, INVALID_RESPONSE_CODE, message, request_id))


def _error_body(response: requests.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else None


def _safe_error_code(response: requests.Response) -> str | None:
    """The Graph error code if it is plainly a code, never free text."""
    error = _error_body(response)
    code = error.get("code") if error else None
    if code is None:
        return None
    if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code):
        return code
    return "UNCLASSIFIED_ERROR"


def _first_string(source: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _error_from(response: requests.Response) -> GraphError:
    code = REQUEST_FAILED_CODE
    message = f"Microsoft Graph request failed with status {response.status_code}."
    request_id = response.headers.get("request-id")
    client_request_id = response.headers.get("client-request-id")
    error = _error_body(response)
    if error is not None:
        code = _first_string(error, "code") or code
        message = _first_string(error, "message") or message
        inner = error.get("innerError") or error.get("innererror")
        if isinstance(inner, dict):
            request_id = _first_string(inner, "request-id", "requestId") or request_id
            client_request_id = (
                _first_string(inner, "client-request-id", "clientRequestId") or client_request_id
            )
    return GraphError(
        GraphErrorDetails(response.status_code, code, message, request_id, client_request_id),
        retry_after=retry_after_header(response.headers),
    )
