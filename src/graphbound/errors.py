"""Errors raised by the Graph transport."""

from __future__ import annotations

from dataclasses import dataclass

RETRY_EXHAUSTED_CODE = "GRAPH_RETRY_EXHAUSTED"
INVALID_RESPONSE_CODE = "INVALID_RESPONSE"
UNEXPECTED_REDIRECT_CODE = "UNEXPECTED_REDIRECT"
REQUEST_FAILED_CODE = "GRAPH_REQUEST_FAILED"


@dataclass(frozen=True)
class GraphErrorDetails:
    """What Microsoft Graph said about a failed request.

    ``code`` and ``message`` come from the Graph error body when it has one.
    ``request_id`` is the value Microsoft support asks for.
    """

    status_code: int
    code: str
    message: str
    request_id: str | None = None
    client_request_id: str | None = None


class GraphError(Exception):
    """A request reached Graph (or a local limit) and did not succeed.

    ``retry_after`` is the raw ``Retry-After`` header when Graph sent one, so a
    caller that owns its own retry loop can honor the server's cooldown.
    """

    def __init__(self, details: GraphErrorDetails, *, retry_after: str | None = None) -> None:
        self.details = details
        self.retry_after = retry_after
        super().__init__(f"Microsoft Graph {details.code}: {details.message}")


class GraphRetryExhausted(GraphError):
    """Retrying further would exceed an attempt limit, a deadline, or a sleep budget.

    ``details.status_code`` is the last response status (even a late success),
    or 503 when the last attempt never produced a response. ``reason`` is
    ``attempts``, ``elapsed``, ``sleep`` or ``delay``. ``attempts`` counts sends
    when raised by GraphClient; direct budget/policy calls leave it unset.
    """

    def __init__(
        self,
        status_code: int = 503,
        *,
        retry_after: str | None = None,
        reason: str = "elapsed",
        request_id: str | None = None,
        client_request_id: str | None = None,
        attempts: int | None = None,
    ) -> None:
        self.reason = reason
        self.attempts = attempts
        super().__init__(
            GraphErrorDetails(
                status_code,
                RETRY_EXHAUSTED_CODE,
                f"Retry budget exhausted ({reason}).",
                request_id,
                client_request_id,
            ),
            retry_after=retry_after,
        )


class UnsafeGraphUrl(ValueError):
    """A request was refused before sending because of where or how it would go."""


class InvalidGraphObjectId(ValueError):
    """A value is not a canonical directory object ID."""

    code = "INVALID_OBJECT_ID"

    def __init__(self) -> None:
        super().__init__("A canonical Microsoft directory object ID is required.")
