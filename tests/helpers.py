"""Shared test doubles. Nothing here touches the network."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import requests
from requests.adapters import BaseAdapter

USER_ID = "11111111-1111-4111-8111-111111111111"
GROUP_ID = "22222222-2222-4222-8222-222222222222"


class FakeClock:
    """A clock that only moves when something sleeps or a test advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_response(
    status_code: int,
    body: Any = None,
    *,
    headers: dict[str, str] | None = None,
    content: bytes = b"json",
) -> Mock:
    """A stand-in ``requests.Response``. Pass an exception as ``body`` to make ``json()`` raise."""
    response = Mock(spec=requests.Response)
    response.status_code = status_code
    response.ok = 200 <= status_code < 400
    response.headers = headers or {}
    response.content = content
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    return response


def mock_session(*responses: Any) -> Mock:
    """A session whose ``request`` returns (or raises) each of ``responses`` in turn."""
    session = Mock(spec=requests.Session)
    session.request.side_effect = list(responses)
    return session


class CapturingAdapter(BaseAdapter):
    """Records the prepared requests a real ``requests.Session`` would put on the wire."""

    def __init__(self, *bodies: dict[str, Any]) -> None:
        super().__init__()
        self.sent: list[requests.PreparedRequest] = []
        self._bodies = list(bodies)

    def send(self, request: requests.PreparedRequest, **_kwargs: Any) -> requests.Response:
        self.sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url or ""
        body = self._bodies.pop(0) if self._bodies else {"value": []}
        response._content = json.dumps(body).encode()
        return response

    def close(self) -> None:
        pass

    @property
    def urls(self) -> list[str | None]:
        return [request.url for request in self.sent]


def wire_session(*bodies: dict[str, Any]) -> tuple[requests.Session, CapturingAdapter]:
    """A real session with every scheme routed to a :class:`CapturingAdapter`."""
    adapter = CapturingAdapter(*bodies)
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session, adapter
