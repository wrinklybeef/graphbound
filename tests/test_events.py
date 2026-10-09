from dataclasses import asdict
from unittest.mock import Mock

import pytest
import requests

from graphbound import GraphClient, ReadRetryPolicy, RequestFailure, RetryScheduled, read_budget
from tests.helpers import FakeClock, make_response, mock_session


def observed(session, **kwargs):
    failures, retries = [], []
    client = GraphClient(
        "token", session=session, on_failure=failures.append, on_retry=retries.append, **kwargs
    )
    return client, failures, retries


def test_read_retry_reports_the_failure_then_the_scheduled_retry():
    clock = FakeClock()
    error = {"error": {"code": "serviceNotAvailable", "message": "internal detail"}}
    session = mock_session(
        make_response(503, error, headers={"Retry-After": "2"}), make_response(200, {})
    )
    client, failures, retries = observed(session, read_policy=ReadRetryPolicy())

    with read_budget(clock=clock, sleep=clock.sleep):
        client.get("users")

    assert failures == [
        RequestFailure(method="GET", attempt=1, status_code=503, error_code="serviceNotAvailable")
    ]
    assert retries == [RetryScheduled(attempt=1, delay_seconds=2.0, status_code=503)]


def test_transport_error_reports_the_exception_type_only():
    clock = FakeClock()
    session = mock_session(requests.ConnectionError("dns for secret-host"), make_response(200, {}))
    client, failures, retries = observed(session, read_policy=ReadRetryPolicy())

    with read_budget(clock=clock, sleep=clock.sleep):
        client.get("users")

    assert failures == [RequestFailure(method="GET", attempt=1, exception_type="ConnectionError")]
    assert "secret-host" not in str(failures)
    assert [event.status_code for event in retries] == [503]


def test_final_failure_is_reported_without_a_retry():
    session = mock_session(make_response(403, {"error": {"code": "Forbidden", "message": "no"}}))
    client, failures, retries = observed(session)

    with pytest.raises(Exception, match="Forbidden"):
        client.delete("users/user-1")

    assert failures == [
        RequestFailure(method="DELETE", attempt=1, status_code=403, error_code="Forbidden")
    ]
    assert retries == []


def test_throttle_retry_is_reported_for_writes():
    session = mock_session(
        make_response(429, {}, headers={"Retry-After": "4"}), make_response(200, {})
    )
    client, failures, retries = observed(session, sleep=Mock())

    client.post("users", json={})

    assert failures == [RequestFailure(method="POST", attempt=1, status_code=429)]
    assert retries == [RetryScheduled(attempt=1, delay_seconds=4.0, status_code=429)]


def test_timeout_on_a_write_is_reported_then_raised():
    session = mock_session(requests.Timeout())
    client, failures, _ = observed(session)

    with pytest.raises(requests.Timeout):
        client.patch("users/user-1", json={})

    assert failures == [RequestFailure(method="PATCH", attempt=1, exception_type="Timeout")]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": {"code": "Request_ResourceNotFound"}}, "Request_ResourceNotFound"),
        ({"error": {"code": "free text with spaces and a secret"}}, "UNCLASSIFIED_ERROR"),
        ({"error": {"code": 42}}, "UNCLASSIFIED_ERROR"),
        ({"error": {"message": "no code"}}, None),
        ({"error": "not an object"}, None),
        (["not", "an", "object"], None),
        (ValueError("not json"), None),
    ],
)
def test_error_code_is_only_ever_a_plain_code(body, expected):
    session = mock_session(make_response(404, body))
    client, failures, _ = observed(session)

    with pytest.raises(Exception, match="Graph"):
        client.get("users/user-1")

    assert failures[0].error_code == expected


def test_events_have_no_field_that_could_hold_a_url_body_or_message():
    assert set(asdict(RequestFailure("GET", 1))) == {
        "method",
        "attempt",
        "status_code",
        "error_code",
        "exception_type",
    }
    assert set(asdict(RetryScheduled(1, 1.0, 503))) == {"attempt", "delay_seconds", "status_code"}


def test_no_callbacks_means_error_bodies_are_not_parsed_for_events():
    response = make_response(500, {"error": {"code": "x", "message": "y"}})
    session = mock_session(response)

    with pytest.raises(Exception, match="Graph"):
        GraphClient("token", session=session).post("users", json={})

    assert response.json.call_count == 1
