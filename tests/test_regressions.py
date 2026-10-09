"""Failure cases from the public-release review. No network or real sleeps."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import graphbound
from graphbound import (
    BudgetHttpSession,
    GraphClient,
    GraphError,
    GraphRetryExhausted,
    ReadBudget,
    ReadRetryPolicy,
    UnsafeGraphUrl,
    read_budget,
)
from tests.helpers import FakeClock, make_response, mock_session, wire_session

V1 = "https://graph.microsoft.com/v1.0/users"


@pytest.mark.parametrize("link", [None, False, 1, [], {}, "", " ", "users", "//host/users"])
def test_present_malformed_continuation_never_means_complete(link):
    session = mock_session(make_response(200, {"value": [], "@odata.nextLink": link}))
    with pytest.raises(GraphError, match="nextLink"):
        list(GraphClient("token", session=session).paginate("users"))
    assert session.request.call_count == 1


def test_empty_page_with_continuation_is_followed():
    session, adapter = wire_session(
        {"value": [], "@odata.nextLink": V1 + "?$skiptoken=next"},
        {"value": [{"id": "last"}]},
    )
    assert list(GraphClient("token", session=session).paginate("users")) == [{"id": "last"}]
    assert len(adapter.sent) == 2


@pytest.mark.parametrize("cycle_length", [1, 2])
def test_pagination_cycle_stops_before_refetching_a_page(cycle_length):
    first = "https://GRAPH.MICROSOFT.COM:443/v1.0/users?%24select=id"
    second = V1 + "?$skiptoken=next"
    pages = [{"value": [], "@odata.nextLink": first}]
    if cycle_length == 2:
        pages.insert(0, {"value": [], "@odata.nextLink": second})
    session, adapter = wire_session(*pages)
    with pytest.raises(GraphError, match="cycle"):
        list(GraphClient("token", session=session).paginate("users", params={"$select": "id"}))
    assert len(adapter.sent) == cycle_length


def test_malformed_absolute_continuation_uses_safe_url_error():
    session = mock_session(make_response(200, {"value": [], "@odata.nextLink": "https://[secret"}))
    with pytest.raises(UnsafeGraphUrl) as caught:
        list(GraphClient("token", session=session).paginate("users"))
    assert "secret" not in str(caught.value)
    assert session.request.call_count == 1


@pytest.mark.parametrize("method", ["get", "post", "patch", "delete"])
@pytest.mark.parametrize(("seconds", "max_sleep", "reason"), [(1, 10, "elapsed"), (60, 0, "sleep")])
def test_default_throttling_refuses_unaffordable_sleep(method, seconds, max_sleep, reason):
    clock = FakeClock()
    session = mock_session(
        make_response(429, {}, headers={"Retry-After": "2", "request-id": "rid"})
    )
    client_sleep = Mock()
    with (
        read_budget(seconds, max_sleep=max_sleep, clock=clock, sleep=clock.sleep),
        pytest.raises(GraphRetryExhausted) as caught,
    ):
        getattr(GraphClient("token", session=session, sleep=client_sleep), method)("users")
    assert caught.value.reason == reason
    assert caught.value.details.status_code == 429
    assert caught.value.details.request_id == "rid"
    assert caught.value.retry_after == "2"
    assert caught.value.attempts == 1
    assert clock.sleeps == []
    client_sleep.assert_not_called()
    assert session.request.call_count == 1


def test_default_throttling_charges_each_enclosing_budget():
    clock = FakeClock()
    session = mock_session(
        make_response(429, {}, headers={"Retry-After": "2"}), make_response(200, {})
    )
    with (
        read_budget(30, clock=clock, sleep=clock.sleep) as parent,
        read_budget(20, clock=clock, sleep=clock.sleep) as child,
    ):
        GraphClient("token", session=session).post("users")
        assert child.sleep_seconds == parent.sleep_seconds == 2
        assert child.retry_count == parent.retry_count == 1
    assert clock.sleeps == [2]


@pytest.mark.parametrize(
    ("policy", "max_sleep", "header", "reason"),
    [
        (ReadRetryPolicy(max_attempts=1), 90, "1", "attempts"),
        (ReadRetryPolicy(max_elapsed=1), 90, "2", "elapsed"),
        (ReadRetryPolicy(), 0, "1", "sleep"),
        (ReadRetryPolicy(), 90, "31", "delay"),
    ],
)
def test_read_exhaustion_retains_response_diagnostics(policy, max_sleep, header, reason):
    clock = FakeClock()
    body = {
        "error": {
            "message": "sensitive provider text",
            "innerError": {
                "request-id": "inner-id",
                "client-request-id": "client-id",
            },
        }
    }
    session = mock_session(
        make_response(503, body, headers={"Retry-After": header, "request-id": "outer"})
    )
    with (
        read_budget(60, max_sleep=max_sleep, clock=clock, sleep=clock.sleep),
        pytest.raises(GraphRetryExhausted) as caught,
    ):
        GraphClient("token", session=session, read_policy=policy).get("users")
    error = caught.value
    assert error.details.status_code == 503
    assert error.details.request_id == "inner-id"
    assert error.details.client_request_id == "client-id"
    assert error.retry_after == header
    assert error.reason == reason
    assert error.attempts == 1
    assert "sensitive" not in str(error)
    assert clock.sleeps == []


def test_direct_policy_wait_keeps_status_and_header_on_sleep_exhaustion():
    clock = FakeClock()
    budget = ReadBudget(60, max_sleep=0, clock=clock, sleep=clock.sleep)
    with pytest.raises(GraphRetryExhausted) as caught:
        ReadRetryPolicy().wait(1, 0, budget, status=502, headers={"Retry-After": "1"})
    assert caught.value.details.status_code == 502
    assert caught.value.retry_after == "1"
    assert caught.value.reason == "sleep"


@pytest.mark.parametrize("read_policy", [None, ReadRetryPolicy()])
def test_late_success_is_discarded_with_its_correlation_id(read_policy):
    clock = FakeClock()
    session = mock_session()

    def slow_response(*_args, **_kwargs):
        clock.now += 5
        return make_response(200, {}, headers={"request-id": "late"})

    session.request.side_effect = slow_response
    with (
        read_budget(1, clock=clock, sleep=clock.sleep),
        pytest.raises(GraphRetryExhausted) as caught,
    ):
        GraphClient("token", session=session, read_policy=read_policy).get("users")
    assert caught.value.reason == "elapsed"
    assert caught.value.details.status_code == 200
    assert caught.value.details.request_id == "late"
    assert clock.now == 5  # Cooperative checks do not cancel the blocking call.


def test_transport_failure_does_not_reuse_an_earlier_response_id():
    clock = FakeClock()
    session = mock_session(
        make_response(429, {}, headers={"Retry-After": "0", "request-id": "earlier"}),
        requests.Timeout("transport failure"),
    )
    with (
        read_budget(60, clock=clock, sleep=clock.sleep),
        pytest.raises(GraphRetryExhausted) as caught,
    ):
        GraphClient("token", session=session, read_policy=ReadRetryPolicy(max_attempts=2)).get(
            "users"
        )
    assert caught.value.details.status_code == 503
    assert caught.value.details.request_id is None
    assert caught.value.attempts == 2
    assert isinstance(caught.value.__context__, requests.Timeout)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_transport_timeouts_must_be_finite_and_positive(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        GraphClient("token", timeout=timeout)
    with pytest.raises(ValueError, match="finite and positive"):
        BudgetHttpSession(ReadBudget(), timeout=timeout)


@pytest.mark.parametrize("token_ok", [True, False])
def test_readme_quick_start_is_read_only_and_handles_empty_results(monkeypatch, token_ok):
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    example = readme.split("## Quick start", 1)[1].split("```python\n", 1)[1].split("```", 1)[0]
    for name in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
        monkeypatch.setenv(name, "synthetic")
    acquire = Mock(
        return_value={"access_token": "token"} if token_ok else {"error": "invalid_client"}
    )
    monkeypatch.setitem(
        sys.modules,
        "msal",
        SimpleNamespace(
            ConfidentialClientApplication=Mock(
                return_value=SimpleNamespace(acquire_token_for_client=acquire)
            ),
        ),
    )
    session, adapter = wire_session({"value": []})
    factory = Mock(side_effect=lambda token, **kw: GraphClient(token, session=session, **kw))
    monkeypatch.setattr(graphbound, "GraphClient", factory)
    if token_ok:
        exec(compile(example, "README quick start", "exec"), {})
        assert [request.method for request in adapter.sent] == ["GET"]
    else:
        with pytest.raises(SystemExit, match="Token acquisition failed"):
            exec(compile(example, "README quick start", "exec"), {})
        factory.assert_not_called()
