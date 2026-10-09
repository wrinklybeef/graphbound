"""Bounded retries for Graph reads. Mutations are never replayed by policy."""

from __future__ import annotations

import math
import random
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from graphbound.errors import GraphRetryExhausted
from graphbound.events import RetryScheduled

Clock = Callable[[], float]
Sleep = Callable[[float], None]

TRANSIENT_READ_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_BUDGET_SECONDS = 180.0
DEFAULT_MAX_SLEEP_SECONDS = 90.0

_RETRY_AFTER_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_MAX_RETRY_AFTER_LENGTH = 128

_active_budget: ContextVar[ReadBudget | None] = ContextVar("graphbound_read_budget", default=None)


class ReadBudget:
    """A cooperative deadline plus a cap on requested retry sleep.

    A child shares its parent's remaining time, and pauses charge every ancestor.
    """

    def __init__(
        self,
        seconds: float = DEFAULT_BUDGET_SECONDS,
        *,
        max_sleep: float = DEFAULT_MAX_SLEEP_SECONDS,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
        parent: ReadBudget | None = None,
    ) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("A read budget must be finite and positive.")
        if not math.isfinite(max_sleep) or max_sleep < 0:
            raise ValueError("A sleep limit must be finite and nonnegative.")
        self.clock = clock
        self.sleep = sleep
        self.parent = parent
        self.started = clock()
        self.deadline = self.started + seconds
        self.max_sleep = max_sleep
        self.sleep_seconds = 0.0
        self.retry_count = 0

    def remaining(self) -> float:
        """Seconds left before this budget or any ancestor expires.

        Raises :class:`GraphRetryExhausted` when nothing is left.
        """
        remaining = self.deadline - self.clock()
        if self.parent is not None:
            remaining = min(remaining, self.parent.remaining())
        if remaining <= 0:
            raise GraphRetryExhausted()
        return remaining

    def pause(self, delay: float, status: int = 503) -> None:
        """Sleep ``delay`` seconds, or raise without sleeping if any budget cannot afford it."""
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("A retry delay must be finite and nonnegative.")
        chain: list[ReadBudget] = []
        current: ReadBudget | None = self
        while current is not None:
            if delay >= current.remaining():
                raise GraphRetryExhausted(status, reason="elapsed")
            if current.sleep_seconds + delay > current.max_sleep:
                raise GraphRetryExhausted(status, reason="sleep")
            chain.append(current)
            current = current.parent
        for budget in chain:
            budget.sleep_seconds += delay
            budget.retry_count += 1
        self.sleep(delay)
        self.remaining()


def current_read_budget() -> ReadBudget | None:
    """The innermost active :func:`read_budget`, if any."""
    return _active_budget.get()


@contextmanager
def read_budget(
    seconds: float = DEFAULT_BUDGET_SECONDS,
    *,
    max_sleep: float = DEFAULT_MAX_SLEEP_SECONDS,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> Iterator[ReadBudget]:
    """Apply one cooperative deadline to Graph calls inside the block.

    Checks run between requests and sleeps; they cannot interrupt a blocking
    Requests call. Its timeout limits socket inactivity, not total download time.

    The budget is held in a :class:`~contextvars.ContextVar`, so it follows the
    current thread or task. A thread started inside the block does not inherit
    it unless the caller copies the context.
    """
    budget = ReadBudget(
        seconds, max_sleep=max_sleep, clock=clock, sleep=sleep, parent=current_read_budget()
    )
    marker = _active_budget.set(budget)
    try:
        yield budget
    finally:
        _active_budget.reset(marker)


def retry_after_header(headers: object) -> str | None:
    """The ``Retry-After`` value from a header mapping, matched case-insensitively."""
    if not isinstance(headers, Mapping):
        return None
    for name, value in headers.items():
        if str(name).casefold() == "retry-after":
            return value if isinstance(value, str) else None
    return None


def _retry_after_seconds(value: str | None, wall_clock: Clock) -> float | None:
    """Parse delta-seconds or an HTTP date. Anything else is treated as absent."""
    if value is None or len(value) > _MAX_RETRY_AFTER_LENGTH:
        return None
    value = value.strip()
    if _RETRY_AFTER_SECONDS.fullmatch(value):
        return float(value)
    try:
        date = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return max(0.0, date.timestamp() - wall_clock())


@dataclass(frozen=True)
class ReadRetryPolicy:
    """How hard to retry one idempotent read.

    ``max_attempts`` counts the first try. ``max_elapsed`` is a cooperative
    deadline for requests and sleeps together. ``max_delay`` is the longest single
    wait the policy will accept from the server or compute for itself.
    """

    max_attempts: int = 4
    max_elapsed: float = 90.0
    max_delay: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("A retry policy needs at least one attempt.")
        if any(not math.isfinite(v) or v <= 0 for v in (self.max_elapsed, self.max_delay)):
            raise ValueError("Retry limits must be finite and positive.")

    def delay(
        self,
        headers: object,
        attempt: int,
        *,
        status: int = 429,
        wall_clock: Clock = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> float:
        """Seconds to wait before the attempt after ``attempt``.

        A valid ``Retry-After`` wins. If it asks for longer than ``max_delay``
        the policy raises instead of retrying early, because retrying inside a
        server cooldown extends the throttling. Without a usable header the
        delay is exponential with a little jitter.
        """
        value = retry_after_header(headers)
        seconds = _retry_after_seconds(value, wall_clock)
        if seconds is not None:
            if not math.isfinite(seconds) or seconds > self.max_delay:
                raise GraphRetryExhausted(status, retry_after=value, reason="delay")
            return seconds
        backoff: float = 2.0 ** min(attempt - 1, 10)
        return min(self.max_delay, backoff + jitter() * 0.25)

    def wait(
        self,
        attempt: int,
        started: float,
        budget: ReadBudget,
        *,
        status: int = 503,
        headers: object = None,
        delay: float | None = None,
        on_retry: Callable[[RetryScheduled], None] | None = None,
    ) -> float:
        """Sleep before the next attempt, or raise if another attempt is not allowed.

        ``started`` is the ``budget.clock()`` reading taken before the first
        attempt. Returns the seconds slept.
        """
        retry_after = retry_after_header(headers)
        if attempt >= self.max_attempts:
            raise GraphRetryExhausted(status, retry_after=retry_after, reason="attempts")
        pause = self.delay(headers, attempt, status=status) if delay is None else delay
        if budget.clock() - started + pause >= self.max_elapsed:
            raise GraphRetryExhausted(status, retry_after=retry_after)
        if on_retry is not None:
            on_retry(RetryScheduled(attempt=attempt, delay_seconds=pause, status_code=status))
        try:
            budget.pause(pause, status)
        except GraphRetryExhausted as exc:
            raise GraphRetryExhausted(status, retry_after=retry_after, reason=exc.reason) from exc
        return pause


class BudgetHttpSession:
    """An HTTP client for MSAL whose timeouts shrink with a :class:`ReadBudget`.

    Pass it as ``http_client`` to ``msal.ConfidentialClientApplication`` so that
    token acquisition uses its cooperative deadline. Blocking transport calls
    can exceed it; late responses are discarded. Token requests are not retried.
    """

    def __init__(
        self,
        budget: ReadBudget,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("An HTTP timeout must be finite and positive.")
        self.budget = budget
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs["timeout"] = min(self.timeout, self.budget.remaining())
        response = self.session.request(method, url, **kwargs)
        self.budget.remaining()
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, **kwargs)

    def close(self) -> None:
        self.session.close()
