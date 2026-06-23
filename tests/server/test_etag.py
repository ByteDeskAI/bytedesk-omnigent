"""Unit tests for parse_if_match — the If-Match header → version parser (BDP-2412)."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.etag import parse_if_match


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("*", None),
        ("7", 7),
        ('"7"', 7),
        ('W/"7"', 7),
        ("  42  ", 42),
    ],
)
def test_parse_if_match_valid(header: str | None, expected: int | None) -> None:
    assert parse_if_match(header) == expected


@pytest.mark.parametrize("header", ['"abc"', "not-an-int", '"1.5"', '"7x"'])
def test_parse_if_match_malformed_fails_closed(header: str) -> None:
    # a present-but-unparseable If-Match is a client error (400), never
    # silently treated as "no precondition"
    with pytest.raises(OmnigentError):
        parse_if_match(header)
