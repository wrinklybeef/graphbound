"""Events passed to the optional ``on_failure`` and ``on_retry`` callbacks.

The events carry no URL, request body, response body, provider message, or
credential, so they can be logged as they are.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RequestFailure:
    """One attempt failed, before any decision about retrying it.

    ``status_code`` and ``error_code`` are set when Graph answered;
    ``exception_type`` is set when the attempt never got a response.
    ``error_code`` is the Graph error code, restricted to a safe character set.
    """

    method: str
    attempt: int
    status_code: int | None = None
    error_code: str | None = None
    exception_type: str | None = None


@dataclass(frozen=True)
class RetryScheduled:
    """The client is about to sleep ``delay_seconds`` and then try again."""

    attempt: int
    delay_seconds: float
    status_code: int
