from datetime import UTC, datetime
from email.utils import format_datetime
from unittest.mock import Mock

import pytest
import requests

from graphbound import (
    BudgetHttpSession,
    GraphClient,
    GraphError,
    GraphRetryExhausted,
    ReadBudget,
    ReadRetryPolicy,
    current_read_budget,
    read_budget,
)
from tests.helpers import FakeClock, make_response, mock_session


def reading_client(session, **kwargs):
    return GraphClient("token", session=session, read_policy=ReadRetryPolicy(), **kwargs)


class TestRetryAfter:
    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"retry-after": "2"}, 2),
            ({"Retry-After": "0.5"}, 0.5),
            ({"Retry-After": " 3 "}, 3),
            ({"Retry-After": "garbage"}, 1),
            ({"Retry-After": "NaN"}, 1),
            ({"Retry-After": "inf"}, 1),
            ({"Retry-After": "-2"}, 1),
            ({"Retry-After": "1e3"}, 1),
            ({"Retry-After": 5}, 1),
            ([], 1),
            (None, 1),
            ({}, 1),
        ],
    )
    def test_valid_values_win_and_everything_else_falls_back(self, headers, expected):
        assert ReadRetryPolicy().delay(headers, 1, jitter=lambda: 0) == expected

    def test_http_date_is_measured_against_the_wall_clock(self):
        date = format_datetime(datetime.fromtimestamp(1020, UTC), usegmt=True)

        assert ReadRetryPolicy().delay({"Retry-After": date}, 1, wall_clock=lambda: 1000) == 20

    def test_http_date_without_a_zone_is_read_as_utc(self):
        headers = {"Retry-After": "Thu, 01 Jan 1970 00:17:00 -0000"}

        assert ReadRetryPolicy().delay(headers, 1, wall_clock=lambda: 1000) == 20

    def test_http_date_in_the_past_means_retry_now(self):
        date = format_datetime(datetime.fromtimestamp(900, UTC), usegmt=True)

        assert ReadRetryPolicy().delay({"Retry-After": date}, 1, wall_clock=lambda: 1000) == 0

    @pytest.mark.parametrize("value", ["31", "999999", "9" * 128])
    def test_cooldown_beyond_the_cap_is_refused_instead_of_retried_early(self, value):
        with pytest.raises(GraphRetryExhausted) as caught:
            ReadRetryPolicy().delay({"Retry-After": value}, 1)

        assert caught.value.details.code == "GRAPH_RETRY_EXHAUSTED"
        assert caught.value.details.status_code == 429
        assert caught.value.retry_after == value

    def test_overlong_header_is_ignored(self):
        assert ReadRetryPolicy().delay({"Retry-After": "9" * 129}, 1, jitter=lambda: 0) == 1

    def test_backoff_doubles_and_is_capped(self):
        policy = ReadRetryPolicy(max_delay=10)

        assert [policy.delay(None, n, jitter=lambda: 0) for n in (1, 2, 3, 4, 5, 60)] == [
            1,
            2,
            4,
            8,
            10,
            10,
        ]

    def test_jitter_adds_at_most_a_quarter_second(self):
        assert ReadRetryPolicy().delay(None, 1, jitter=lambda: 1) == 1.25


class TestReadRetries:
    @pytest.mark.parametrize(
        "first",
        [429, 500, 502, 503, 504, requests.Timeout(), requests.ConnectionError()],
        ids=lambda value: str(value) if isinstance(value, int) else type(value).__name__,
    )
    def test_transient_failures_are_retried_with_the_same_token(self, first):
        clock = FakeClock()
        failure = (
            make_response(first, {}, headers={"Retry-After": "2"})
            if isinstance(first, int)
            else first
        )
        session = mock_session(failure, make_response(200, {"value": []}))

        with read_budget(clock=clock, sleep=clock.sleep):
            assert reading_client(session).get("users") == {"value": []}

        assert session.request.call_count == 2
        assert len(clock.sleeps) == 1
        assert clock.sleeps[0] <= 2
        tokens = {
            call.kwargs["headers"]["Authorization"] for call in session.request.call_args_list
        }
        assert tokens == {"Bearer token"}

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_deterministic_read_errors_are_not_retried(self, status):
        session = mock_session(make_response(status, {}))

        with pytest.raises(GraphError):
            reading_client(session).get("users")

        assert session.request.call_count == 1

    @pytest.mark.parametrize("method", ["post", "patch", "delete"])
    @pytest.mark.parametrize("failure", [503, requests.Timeout()], ids=["503", "Timeout"])
    def test_writes_are_never_replayed_after_a_possible_side_effect(self, method, failure):
        outcome = make_response(failure, {}) if isinstance(failure, int) else failure
        session = mock_session(outcome)

        with pytest.raises((GraphError, requests.Timeout)):
            getattr(reading_client(session), method)("users")

        assert session.request.call_count == 1

    def test_attempt_limit(self):
        clock = FakeClock()
        session = Mock(spec=requests.Session)
        session.request.return_value = make_response(503, {})

        with read_budget(clock=clock, sleep=clock.sleep), pytest.raises(GraphRetryExhausted) as e:
            reading_client(session).get("users")

        assert e.value.details.status_code == 503
        assert session.request.call_count == 4
        assert len(clock.sleeps) == 3
        assert clock.now < 8

    def test_transport_errors_exhaust_into_a_graph_error_that_keeps_the_cause(self):
        clock = FakeClock()
        session = Mock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("reset")

        with read_budget(clock=clock, sleep=clock.sleep), pytest.raises(GraphRetryExhausted) as e:
            reading_client(session).get("users")

        assert session.request.call_count == 4
        assert isinstance(e.value.__context__, requests.ConnectionError)

    def test_absurd_retry_after_never_sleeps(self):
        clock = FakeClock()
        session = mock_session(make_response(429, {}, headers={"Retry-After": "999999"}))

        with read_budget(clock=clock, sleep=clock.sleep), pytest.raises(GraphRetryExhausted) as e:
            reading_client(session).get("users")

        assert e.value.retry_after == "999999"
        assert session.request.call_count == 1
        assert clock.sleeps == []

    def test_without_an_ambient_budget_the_client_sleep_is_used(self):
        sleep = Mock()
        session = mock_session(
            make_response(503, {}, headers={"Retry-After": "3"}), make_response(200, {})
        )

        assert reading_client(session, sleep=sleep).get("users") == {}

        sleep.assert_called_once_with(3.0)

    def test_custom_policy_limits_attempts(self):
        clock = FakeClock()
        session = Mock(spec=requests.Session)
        session.request.return_value = make_response(500, {})
        client = GraphClient("token", session=session, read_policy=ReadRetryPolicy(max_attempts=2))

        with read_budget(clock=clock, sleep=clock.sleep), pytest.raises(GraphRetryExhausted):
            client.get("users")

        assert session.request.call_count == 2


class TestBudgets:
    def test_request_timeout_shrinks_to_the_budget_and_a_late_answer_is_discarded(self):
        clock = FakeClock()
        session = Mock(spec=requests.Session)

        def slow(*_args, **_kwargs):
            clock.now += 6
            return make_response(200, {})

        session.request.side_effect = slow

        with read_budget(5, clock=clock, sleep=clock.sleep), pytest.raises(GraphRetryExhausted):
            reading_client(session).get("users")

        assert session.request.call_args.kwargs["timeout"] == 5
        assert session.request.call_count == 1

    def test_budget_also_bounds_requests_that_have_no_read_policy(self):
        clock = FakeClock()
        session = mock_session(make_response(200, {}))

        with read_budget(4, clock=clock, sleep=clock.sleep):
            GraphClient("token", session=session).post("users", json={})

        assert session.request.call_args.kwargs["timeout"] == 4

    def test_expired_budget_stops_before_sending(self):
        clock = FakeClock()
        session = mock_session()

        with read_budget(5, clock=clock, sleep=clock.sleep):
            clock.now = 5
            with pytest.raises(GraphRetryExhausted):
                GraphClient("token", session=session).get("users")

        session.request.assert_not_called()

    def test_policy_elapsed_limit_applies_inside_a_longer_budget(self):
        clock = FakeClock()
        session = Mock(spec=requests.Session)

        def slow_failure(*_args, **_kwargs):
            clock.now += 4
            return make_response(503, {}, headers={"Retry-After": "1"})

        session.request.side_effect = slow_failure
        policy = ReadRetryPolicy(max_attempts=10, max_elapsed=10)
        client = GraphClient("token", session=session, read_policy=policy)

        with read_budget(600, clock=clock, sleep=clock.sleep), pytest.raises(GraphRetryExhausted):
            client.get("users")

        assert session.request.call_count == 2
        assert clock.now < 10

    def test_slow_answer_past_the_policy_limit_is_discarded_inside_a_longer_budget(self):
        clock = FakeClock()
        session = Mock(spec=requests.Session)

        def slow(*_args, **_kwargs):
            clock.now += 11
            return make_response(200, {})

        session.request.side_effect = slow
        client = GraphClient("token", session=session, read_policy=ReadRetryPolicy(max_elapsed=10))

        with read_budget(600, clock=clock, sleep=clock.sleep), pytest.raises(GraphRetryExhausted):
            client.get("users")

        assert session.request.call_args.kwargs["timeout"] == 10
        assert session.request.call_count == 1

    def test_sleep_that_overshoots_the_policy_limit_stops_before_another_request(self):
        clock = FakeClock()

        def oversleep(seconds):
            clock.now += seconds + 100  # lands past the 90 second policy limit

        session = Mock(spec=requests.Session)
        session.request.return_value = make_response(503, {}, headers={"Retry-After": "1"})

        with read_budget(600, clock=clock, sleep=oversleep), pytest.raises(GraphRetryExhausted):
            reading_client(session).get("users")

        assert session.request.call_count == 1

    def test_nested_budgets_charge_every_ancestor(self):
        clock = FakeClock()

        with read_budget(20, max_sleep=3, clock=clock, sleep=clock.sleep) as parent:
            with read_budget(10, clock=clock, sleep=clock.sleep) as child:
                child.pause(2)
                with pytest.raises(GraphRetryExhausted):
                    child.pause(2)
            assert parent.sleep_seconds == 2
            assert parent.retry_count == 1

        assert clock.sleeps == [2]

    def test_child_cannot_outlive_its_parent(self):
        clock = FakeClock()

        with read_budget(5, clock=clock), read_budget(60, clock=clock) as child:
            assert child.remaining() == 5

    def test_pause_longer_than_the_time_left_raises_without_sleeping(self):
        clock = FakeClock()
        budget = ReadBudget(5, clock=clock, sleep=clock.sleep)

        with pytest.raises(GraphRetryExhausted) as caught:
            budget.pause(5, status=429)

        assert caught.value.details.status_code == 429
        assert clock.sleeps == []

    def test_active_budget_is_restored_on_exit_even_after_an_error(self):
        assert current_read_budget() is None
        with read_budget(5) as outer:
            try:
                with read_budget(1) as inner:
                    assert current_read_budget() is inner
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            assert current_read_budget() is outer
        assert current_read_budget() is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"seconds": 0},
            {"seconds": -1},
            {"seconds": float("inf")},
            {"seconds": float("nan")},
            {"seconds": 5, "max_sleep": -1},
            {"seconds": 5, "max_sleep": float("inf")},
        ],
    )
    def test_budget_limits_must_be_finite(self, kwargs):
        with pytest.raises(ValueError, match="finite"):
            ReadBudget(**kwargs)

    @pytest.mark.parametrize("delay", [-1, float("inf"), float("nan")])
    def test_pause_rejects_nonsense_delays(self, delay):
        with pytest.raises(ValueError, match="finite"):
            ReadBudget(5).pause(delay)

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_attempts": 0}, {"max_elapsed": 0}, {"max_delay": float("inf")}],
    )
    def test_policy_limits_are_validated(self, kwargs):
        with pytest.raises(ValueError, match=r"attempt|finite"):
            ReadRetryPolicy(**kwargs)


class TestBudgetHttpSession:
    def test_timeout_is_the_smaller_of_its_own_and_the_budget(self):
        clock = FakeClock()
        session = Mock(spec=requests.Session)
        http = BudgetHttpSession(ReadBudget(5, clock=clock), session=session)

        http.post("https://login.microsoftonline.com/tenant/oauth2/v2.0/token", data={"a": "b"})
        clock.now = 4.5
        http.get("https://login.microsoftonline.com/tenant/v2.0/.well-known/openid-configuration")

        timeouts = [call.kwargs["timeout"] for call in session.request.call_args_list]
        assert timeouts == [5, 0.5]
        assert session.request.call_args_list[0].kwargs["data"] == {"a": "b"}

    def test_expired_budget_refuses_the_token_request(self):
        clock = FakeClock()
        session = Mock(spec=requests.Session)
        http = BudgetHttpSession(ReadBudget(5, clock=clock), session=session)
        clock.now = 6

        with pytest.raises(GraphRetryExhausted):
            http.post("https://login.microsoftonline.com/tenant/oauth2/v2.0/token")

        session.request.assert_not_called()

    def test_close_closes_the_session(self):
        session = Mock(spec=requests.Session)

        BudgetHttpSession(ReadBudget(5), session=session).close()

        session.close.assert_called_once_with()
