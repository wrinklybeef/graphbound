"""Canonical directory object IDs and safe Graph path segments.

Graph addresses many relationships with a trailing ``/$ref``::

    DELETE /groups/{group-id}/members/{user-id}/$ref   removes the membership
    DELETE /groups/{group-id}/members/{user-id}        can delete the user

If ``user-id`` comes from a request body and ends in ``#``, the ``/$ref`` suffix
becomes a URL fragment, is never sent, and the first call turns into the second.
Validating every identifier before it is interpolated closes that off, along
with ``/``, ``?`` and ``..`` injection into the path.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from graphbound.errors import InvalidGraphObjectId

_OBJECT_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def graph_object_id(value: object) -> str:
    """Return ``value`` lowercased if it is a canonical UUID, else raise.

    Rejects every alternate form: braces, missing hyphens, ``urn:uuid:``
    prefixes, surrounding whitespace, user principal names, and non-strings.
    """
    if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
        raise InvalidGraphObjectId()
    return value.lower()


def graph_object_segment(value: object) -> str:
    """Validate ``value`` and return it encoded as exactly one path segment."""
    return quote(graph_object_id(value), safe="")
