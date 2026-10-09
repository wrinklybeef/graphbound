from unittest.mock import Mock

import pytest
import requests

from graphbound import GraphClient, GraphError
from tests.helpers import make_response, mock_session

V1 = "https://graph.microsoft.com/v1.0"
THROTTLED = {"error": {"code": "TooManyRequests", "message": "Slow down"}}
GET_USER = {"id": "1", "method": "GET", "url": "/users/user-1"}


def sent(session: Mock, index: int = -1) -> dict:
    """The keyword arguments of one ``session.request`` call."""
    return session.request.call_args_list[index].kwargs


def test_get_sends_bearer_token_timeout_and_no_redirects():
    session = mock_session(make_response(200, {"value": []}))

    result = GraphClient("token", timeout=12.5, session=session).get("/users")

    assert result == {"value": []}
    session.request.assert_called_once_with(
        "GET",
        f"{V1}/users",
        params=None,
        json=None,
        headers={"Accept": "application/json", "Authorization": "Bearer token"},
        timeout=12.5,
        allow_redirects=False,
    )


@pytest.mark.parametrize("method", ["patch", "delete", "post"])
def test_empty_204_response_is_an_empty_dict(method):
    session = mock_session(make_response(204, None, content=b""))

    assert getattr(GraphClient("token", session=session), method)("/users/user-1") == {}
    assert session.request.call_args.args == (method.upper(), f"{V1}/users/user-1")


def test_post_and_patch_send_json_bodies():
    session = mock_session(make_response(200, {"value": True}), make_response(204, None))
    client = GraphClient("token", session=session)

    assert client.post("/users/user-1/revokeSignInSessions") == {"value": True}
    client.patch("/users/user-1", json={"displayName": "New Name"})

    assert sent(session, 0)["json"] is None
    assert sent(session, 1)["json"] == {"displayName": "New Name"}


def test_caller_headers_are_added_but_cannot_replace_the_token():
    session = mock_session(make_response(200, {}))

    GraphClient("token", session=session).get(
        "/users",
        headers={"ConsistencyLevel": "eventual", "Authorization": "Bearer other"},
    )

    assert sent(session)["headers"] == {
        "Accept": "application/json",
        "ConsistencyLevel": "eventual",
        "Authorization": "Bearer token",
    }


def test_error_carries_graph_code_message_and_request_ids():
    body = {
        "error": {
            "code": "Authorization_RequestDenied",
            "message": "Insufficient privileges.",
            "innerError": {"request-id": "inner-request-id"},
        }
    }
    session = mock_session(
        make_response(403, body, headers={"client-request-id": "client-id", "request-id": "outer"})
    )

    with pytest.raises(GraphError) as caught:
        GraphClient("token", session=session).get("/users")

    details = caught.value.details
    assert details.status_code == 403
    assert details.code == "Authorization_RequestDenied"
    assert details.message == "Insufficient privileges."
    assert details.request_id == "inner-request-id"
    assert details.client_request_id == "client-id"
    assert caught.value.retry_after is None


def test_non_json_error_uses_a_fallback_that_does_not_echo_the_body():
    session = mock_session(make_response(502, ValueError("<html>bad gateway</html>")))

    with pytest.raises(GraphError) as caught:
        GraphClient("token", session=session).get("/users")

    assert caught.value.details.code == "GRAPH_REQUEST_FAILED"
    assert "bad gateway" not in str(caught.value)


@pytest.mark.parametrize("body", [ValueError("not json"), ["a", "list"], "a string"])
def test_successful_response_must_be_a_json_object(body):
    session = mock_session(make_response(200, body))

    with pytest.raises(GraphError) as caught:
        GraphClient("token", session=session).get("/users")

    assert caught.value.details.code == "INVALID_RESPONSE"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_reported_not_followed(status):
    session = mock_session(make_response(status, None, headers={"Location": "https://elsewhere"}))

    with pytest.raises(GraphError) as caught:
        GraphClient("token", session=session).get("/reports/getOffice365ActiveUserDetail")

    assert caught.value.details.code == "UNEXPECTED_REDIRECT"
    assert caught.value.details.status_code == status
    assert session.request.call_count == 1


def test_timeout_is_raised_unchanged_without_a_read_policy():
    session = mock_session(requests.Timeout("timed out"))

    with pytest.raises(requests.Timeout):
        GraphClient("token", session=session).get("/users")

    assert session.request.call_count == 1


class TestThrottling:
    def test_429_waits_for_retry_after_and_tries_once_more(self):
        session = mock_session(
            make_response(429, THROTTLED, headers={"Retry-After": "2"}),
            make_response(200, {"value": []}),
        )
        sleep = Mock()

        assert GraphClient("token", session=session, sleep=sleep).get("/users") == {"value": []}

        sleep.assert_called_once_with(2.0)
        assert session.request.call_count == 2

    def test_a_write_is_replayed_after_429_because_graph_did_not_process_it(self):
        session = mock_session(
            make_response(429, THROTTLED, headers={"Retry-After": "1"}),
            make_response(201, {"id": "new"}),
        )

        result = GraphClient("token", session=session, sleep=Mock()).post("/users", json={})

        assert result == {"id": "new"}

    def test_second_429_is_raised_with_its_retry_after(self):
        session = mock_session(
            make_response(429, THROTTLED, headers={"Retry-After": "1"}),
            make_response(429, THROTTLED, headers={"Retry-After": "7"}),
        )

        with pytest.raises(GraphError) as caught:
            GraphClient("token", session=session, sleep=Mock()).get("/users")

        assert caught.value.details.code == "TooManyRequests"
        assert caught.value.retry_after == "7"
        assert session.request.call_count == 2

    def test_cooldown_longer_than_the_client_waits_surfaces_the_real_429(self):
        session = mock_session(make_response(429, THROTTLED, headers={"Retry-After": "45"}))
        sleep = Mock()

        with pytest.raises(GraphError) as caught:
            GraphClient("token", session=session, sleep=sleep).get("/users")

        assert caught.value.details.status_code == 429
        assert caught.value.details.code == "TooManyRequests"
        assert caught.value.retry_after == "45"
        sleep.assert_not_called()
        assert session.request.call_count == 1

    def test_caller_can_own_throttling(self):
        session = mock_session(make_response(429, THROTTLED, headers={"Retry-After": "2"}))
        sleep = Mock()

        with pytest.raises(GraphError) as caught:
            GraphClient("token", session=session, sleep=sleep, retry_throttling=False).get("/users")

        assert caught.value.retry_after == "2"
        sleep.assert_not_called()
        assert session.request.call_count == 1

    def test_missing_retry_after_waits_one_second(self):
        session = mock_session(make_response(429, THROTTLED), make_response(200, {}))
        sleep = Mock()

        GraphClient("token", session=session, sleep=sleep).get("/users")

        sleep.assert_called_once_with(1.0)


class TestPagination:
    def test_follows_next_link_without_resending_params(self):
        next_link = f"{V1}/users?$skiptoken=next"
        session = mock_session(
            make_response(200, {"value": [{"id": "1"}], "@odata.nextLink": next_link}),
            make_response(200, {"value": [{"id": "2"}]}),
        )

        items = list(GraphClient("token", session=session).paginate("/users", params={"$top": 1}))

        assert items == [{"id": "1"}, {"id": "2"}]
        assert sent(session, 0)["params"] == {"$top": 1}
        assert session.request.call_args_list[1].args[1] == next_link
        assert sent(session, 1)["params"] is None

    def test_pages_are_yielded_one_at_a_time(self):
        session = mock_session(
            make_response(200, {"value": [{"id": "1"}], "@odata.nextLink": f"{V1}/users?p=2"}),
            make_response(200, {"value": []}),
        )

        assert list(GraphClient("token", session=session).paginate_pages("/users")) == [
            [{"id": "1"}],
            [],
        ]

    @pytest.mark.parametrize("page", [{"unexpected": []}, {"value": None}, {"value": ["text"]}])
    def test_malformed_page_raises_instead_of_ending_the_list(self, page):
        session = mock_session(make_response(200, page))

        with pytest.raises(GraphError) as caught:
            list(GraphClient("token", session=session).paginate("/subscribedSkus"))

        assert caught.value.details.code == "INVALID_RESPONSE"


class TestReadBatch:
    def test_posts_the_batch_envelope_to_the_batch_endpoint(self):
        session = mock_session(make_response(200, {"responses": []}))

        GraphClient("token", session=session).read_batch_once([GET_USER])

        assert session.request.call_args.args == ("POST", f"{V1}/$batch")
        assert sent(session)["json"] == {"requests": [GET_USER]}

    def test_beta_batch_endpoint_is_reachable_by_absolute_url(self):
        session = mock_session(make_response(200, {"responses": []}))
        beta = "https://graph.microsoft.com/beta/$batch"

        GraphClient("token", session=session).read_batch_once([GET_USER], path=beta)

        assert session.request.call_args.args[1] == beta

    def test_throttled_batch_is_not_retried_and_exposes_retry_after(self):
        session = mock_session(make_response(429, {}, headers={"Retry-After": "2"}))
        sleep = Mock()

        with pytest.raises(GraphError) as caught:
            GraphClient("token", session=session, sleep=sleep).read_batch_once([GET_USER])

        assert caught.value.retry_after == "2"
        assert session.request.call_count == 1
        sleep.assert_not_called()

    @pytest.mark.parametrize(
        "subrequests",
        [
            [],
            [{"id": "1", "method": "DELETE", "url": "/users/user-1"}],
            [GET_USER, {"id": "2", "method": "PATCH", "url": "/users/user-2"}],
            [GET_USER] * 21,
            ["not a mapping"],
        ],
        ids=["empty", "delete", "mixed", "too-many", "not-mapping"],
    )
    def test_refuses_anything_but_a_small_read_only_batch(self, subrequests):
        session = mock_session()

        with pytest.raises(ValueError, match="batch"):
            GraphClient("token", session=session).read_batch_once(subrequests)

        session.request.assert_not_called()

    def test_refuses_a_non_batch_path(self):
        session = mock_session()

        with pytest.raises(ValueError, match=r"\$batch"):
            GraphClient("token", session=session).read_batch_once([GET_USER], path="/users")

        session.request.assert_not_called()


class TestSessionOwnership:
    def test_injected_session_is_left_open(self):
        session = mock_session()

        with GraphClient("token", session=session):
            pass

        session.close.assert_not_called()

    def test_own_session_is_closed(self, monkeypatch):
        created = Mock(spec=requests.Session)
        monkeypatch.setattr(requests, "Session", lambda: created)

        with GraphClient("token"):
            pass

        created.close.assert_called_once_with()
