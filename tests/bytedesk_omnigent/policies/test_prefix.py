"""Unit tests for the shared :func:`strip_server_prefix` canonicalizer."""

from __future__ import annotations

from bytedesk_omnigent.policies._prefix import strip_server_prefix

_GOOGLE_PREFIXES = ("mcp__google__", "google__")
_GITHUB_PREFIXES = ("mcp__github__", "github__")


def test_strips_first_matching_prefix() -> None:
    """The matching server prefix is removed to yield the canonical name."""
    assert strip_server_prefix("mcp__google__drive_file_get", _GOOGLE_PREFIXES) == "drive_file_get"
    assert strip_server_prefix("google__drive_file_get", _GOOGLE_PREFIXES) == "drive_file_get"


def test_longest_prefix_wins_when_listed_first() -> None:
    """A name carrying both prefixes strips the longer one listed first."""
    # ``mcp__google__`` is listed first, so a bare ``google__`` is not re-stripped
    # off the residue.
    assert (
        strip_server_prefix("mcp__google__sheets_values_get", _GOOGLE_PREFIXES)
        == "sheets_values_get"
    )
    assert (
        strip_server_prefix("mcp__github__create_pull_request", _GITHUB_PREFIXES)
        == "create_pull_request"
    )


def test_no_match_returns_name_unchanged() -> None:
    """A name with no recognized prefix is returned verbatim."""
    assert strip_server_prefix("create_document", _GOOGLE_PREFIXES) == "create_document"
    assert strip_server_prefix("", _GOOGLE_PREFIXES) == ""


def test_only_first_matching_prefix_is_stripped() -> None:
    """Stripping is single-shot: the residue is not re-checked against prefixes."""
    # After removing ``google__`` once, the leftover starts with ``google__`` again;
    # it must NOT be stripped a second time.
    assert strip_server_prefix("google__google__x", ("google__",)) == "google__x"


def test_empty_prefixes_returns_name_unchanged() -> None:
    """No prefixes to try means the name is returned as-is."""
    assert strip_server_prefix("drive_file_get", ()) == "drive_file_get"


def test_accepts_any_iterable_of_prefixes() -> None:
    """Prefixes may be any iterable, not just a tuple."""
    assert strip_server_prefix("github__get_me", ["mcp__github__", "github__"]) == "get_me"
