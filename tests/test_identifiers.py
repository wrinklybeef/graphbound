import pytest
import requests

from graphbound import (
    GraphClient,
    InvalidGraphObjectId,
    UnsafeGraphUrl,
    graph_object_id,
    graph_object_segment,
)
from tests.helpers import GROUP_ID, USER_ID, wire_session


def test_canonical_id_is_returned_lowercased():
    upper = "ABCDEF12-3456-4789-ABCD-EF1234567890"

    assert graph_object_id(upper) == upper.lower()
    assert graph_object_segment(upper) == upper.lower()


@pytest.mark.parametrize(
    "value",
    [
        "",
        USER_ID + "#",
        USER_ID + "/$ref",
        USER_ID + "?x=1",
        USER_ID + "\n",
        " " + USER_ID,
        "{" + USER_ID + "}",
        "urn:uuid:" + USER_ID,
        USER_ID.replace("-", ""),
        USER_ID[:-1],
        USER_ID[:-1] + "g",
        USER_ID[:-1] + "\N{ARABIC-INDIC DIGIT ONE}",  # a Unicode digit is not a hex digit
        "..",
        "adele@contoso.example",
        "%31" + USER_ID[1:],
        None,
        123,
        USER_ID.encode(),
    ],
    ids=repr,
)
def test_everything_else_is_rejected(value):
    with pytest.raises(InvalidGraphObjectId) as caught:
        graph_object_id(value)

    assert caught.value.code == "INVALID_OBJECT_ID"
    with pytest.raises(InvalidGraphObjectId):
        graph_object_segment(value)


def test_rejection_message_does_not_echo_the_input():
    with pytest.raises(InvalidGraphObjectId) as caught:
        graph_object_id("secret-looking-input")

    assert "secret-looking-input" not in str(caught.value)


class TestMembershipRemoval:
    """Why the identifier check exists.

    ``DELETE .../members/{id}/$ref`` removes a membership. Without ``/$ref`` the
    same request addresses the directory object itself.
    """

    hostile_id = USER_ID + "#"

    def test_unvalidated_interpolation_silently_drops_ref(self):
        url = f"https://graph.microsoft.com/v1.0/groups/{GROUP_ID}/members/{self.hostile_id}/$ref"

        prepared = requests.Request("DELETE", url).prepare()

        assert prepared.path_url == f"/v1.0/groups/{GROUP_ID}/members/{USER_ID}"

    def test_validated_segment_refuses_the_hostile_id(self):
        with pytest.raises(InvalidGraphObjectId):
            graph_object_segment(self.hostile_id)

    def test_client_refuses_the_fragment_even_if_validation_was_skipped(self):
        session, adapter = wire_session()

        with pytest.raises(UnsafeGraphUrl):
            GraphClient("token", session=session).delete(
                f"groups/{GROUP_ID}/members/{self.hostile_id}/$ref"
            )

        assert adapter.sent == []

    def test_valid_removal_keeps_ref_in_the_request_that_is_sent(self):
        session, adapter = wire_session()
        group, member = graph_object_segment(GROUP_ID), graph_object_segment(USER_ID)

        GraphClient("token", session=session).delete(f"groups/{group}/members/{member}/$ref")

        assert adapter.sent[0].method == "DELETE"
        assert adapter.sent[0].path_url == f"/v1.0/groups/{GROUP_ID}/members/{USER_ID}/$ref"
