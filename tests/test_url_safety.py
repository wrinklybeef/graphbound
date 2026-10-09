"""What actually goes on the wire, checked through a real ``requests.Session``."""

import pytest

from graphbound import GraphClient, UnsafeGraphUrl
from tests.helpers import wire_session

V1 = "https://graph.microsoft.com/v1.0"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("users", f"{V1}/users"),
        ("/users", f"{V1}/users"),
        ("users?$top=5", f"{V1}/users?$top=5"),
        ("users/adele@contoso.example", f"{V1}/users/adele@contoso.example"),
        (
            "drive/root:/Documents/plan.docx:/children",
            f"{V1}/drive/root:/Documents/plan.docx:/children",
        ),
        ("https://graph.microsoft.com/beta/users", "https://graph.microsoft.com/beta/users"),
        ("HTTPS://GRAPH.MICROSOFT.COM/beta/users", "https://graph.microsoft.com/beta/users"),
        ("https://graph.microsoft.com:443/v1.0/users", f"{V1}/users"),
        # A protocol-relative path is only ever a path on the Graph host.
        ("//evil.example/x", f"{V1}/evil.example/x"),
        ("https:evil.example/x", f"{V1}/https:evil.example/x"),
    ],
)
def test_allowed_paths_reach_only_the_graph_host(path, expected):
    session, adapter = wire_session()

    GraphClient("token", session=session).get(path)

    assert adapter.urls == [expected]
    assert adapter.sent[0].headers["Authorization"] == "Bearer token"


def test_params_are_appended_to_a_query_already_in_the_path():
    session, adapter = wire_session()

    GraphClient("token", session=session).get("users?$top=5", params={"$select": "id"})

    assert adapter.urls == [f"{V1}/users?$top=5&%24select=id"]


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/v1.0/users",
        "https://graph.microsoft.com.evil.example/v1.0/users",
        "https://evil.example/graph.microsoft.com/users",
        "https://graph.microsoft.com@evil.example/users",
        "https://token:secret@graph.microsoft.com/v1.0/users",
        "https://evil.example\\@graph.microsoft.com/users",
        "https://graph.microsoft.com:8443/v1.0/users",
        "https://graph.microsoft.com:notaport/v1.0/users",
        "http://graph.microsoft.com/v1.0/users",
        "ftp://graph.microsoft.com/v1.0/users",
        "https:///v1.0/users",
        "users/abc#/$ref",
        "users/../../beta/users",
        "users/./abc",
        "users/%2e%2e/%2E%2E/beta/users",
        "users\\abc",
        "users/abc\r\nX-Injected: 1",
        "users/abc\x00",
        "\thttps://evil.example/x",
    ],
)
def test_unsafe_paths_are_refused_before_anything_is_sent(path):
    session, adapter = wire_session()

    with pytest.raises(UnsafeGraphUrl):
        GraphClient("token", session=session).get(path)

    assert adapter.sent == []


@pytest.mark.parametrize("method", ["post", "patch", "delete"])
def test_writes_are_checked_the_same_way(method):
    session, adapter = wire_session()

    with pytest.raises(UnsafeGraphUrl):
        getattr(GraphClient("token", session=session), method)("https://evil.example/users")

    assert adapter.sent == []


def test_refusal_message_does_not_echo_the_rejected_url():
    session, _ = wire_session()

    with pytest.raises(UnsafeGraphUrl) as caught:
        GraphClient("token", session=session).get("https://evil.example/x?sig=secret-value")

    assert "secret-value" not in str(caught.value)
    assert "evil.example" not in str(caught.value)


def test_next_link_to_another_host_is_refused_after_the_first_page():
    session, adapter = wire_session(
        {"value": [{"id": "1"}], "@odata.nextLink": "https://evil.example/v1.0/users?page=2"}
    )
    pages = GraphClient("token", session=session).paginate_pages("users")

    assert next(pages) == [{"id": "1"}]
    with pytest.raises(UnsafeGraphUrl):
        next(pages)

    assert adapter.urls == [f"{V1}/users"]


def test_next_link_on_the_graph_host_is_followed():
    session, adapter = wire_session(
        {"value": [{"id": "1"}], "@odata.nextLink": f"{V1}/users?$skiptoken=abc"},
        {"value": [{"id": "2"}]},
    )

    assert list(GraphClient("token", session=session).paginate("users")) == [
        {"id": "1"},
        {"id": "2"},
    ]
    assert adapter.urls == [f"{V1}/users", f"{V1}/users?$skiptoken=abc"]


class TestBaseUrl:
    def test_national_cloud_pins_to_its_own_host(self):
        session, adapter = wire_session()
        client = GraphClient("token", session=session, base_url="https://graph.microsoft.us/v1.0")

        client.get("users")
        with pytest.raises(UnsafeGraphUrl):
            client.get("https://graph.microsoft.com/v1.0/users")

        assert adapter.urls == ["https://graph.microsoft.us/v1.0/users"]

    def test_beta_base_url(self):
        session, adapter = wire_session()
        base_url = "https://graph.microsoft.com/beta/"

        GraphClient("token", session=session, base_url=base_url).get("/users")

        assert adapter.urls == ["https://graph.microsoft.com/beta/users"]

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://graph.microsoft.com/v1.0/",
            "graph.microsoft.com/v1.0/",
            "https://user@graph.microsoft.com/v1.0/",
            "https://graph.microsoft.com/v1.0/?api=1",
            "https://graph.microsoft.com/v1.0/#frag",
            "https://graph.microsoft.com:8443/v1.0/",
        ],
    )
    def test_base_url_must_be_plain_https(self, base_url):
        with pytest.raises(UnsafeGraphUrl):
            GraphClient("token", base_url=base_url)
