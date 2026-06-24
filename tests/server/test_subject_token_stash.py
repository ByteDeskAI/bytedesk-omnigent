"""Unit tests for the per-session subject_token stash (BDP-2434 Part 1).

Office sends the user's MCP access token on the *inbound* ``create_session`` /
``post_event`` routes via ``X-Bytedesk-Subject-Token``; the acting-identity
carrier is minted later on the ``tools/call`` server→runner hop, which does NOT
carry that header. So the inbound routes stash the token per-session in a
bounded TTL cache on ``app.state`` and the mint site reads it back.

Invariants:

- absent header ⇒ nothing stashed, ``get`` ⇒ ``None`` (degrade-to-default);
- the stash is bounded (TTL + maxsize) so it can't grow without limit;
- a session delete evicts its entry;
- the stash is never the source of a log line (asserted by code review; here we
  only prove the value never leaks via ``repr``-ish surfaces is out of scope —
  the functional contract is what we pin).
"""

from __future__ import annotations

from types import SimpleNamespace

from omnigent.server.subject_token_stash import (
    SUBJECT_TOKEN_HEADER,
    evict_subject_token,
    get_subject_token,
    stash_subject_token_from_headers,
)


def _state():
    """A bare ``app.state`` stand-in (the real one is a Starlette ``State``)."""
    return SimpleNamespace()


# ── header → stash ───────────────────────────────────────────────────────────


def test_header_name_is_the_office_contract():
    assert SUBJECT_TOKEN_HEADER == "X-Bytedesk-Subject-Token"


def test_stash_then_get_roundtrips():
    state = _state()
    stash_subject_token_from_headers(state, "conv_1", {SUBJECT_TOKEN_HEADER: "user-tok"})
    assert get_subject_token(state, "conv_1") == "user-tok"


def test_header_lookup_is_case_insensitive():
    # Starlette normalizes header case, but accept a lower-case dict too.
    state = _state()
    stash_subject_token_from_headers(state, "conv_1", {"x-bytedesk-subject-token": "user-tok"})
    assert get_subject_token(state, "conv_1") == "user-tok"


def test_absent_header_stashes_nothing():
    state = _state()
    stash_subject_token_from_headers(state, "conv_1", {"content-type": "application/json"})
    assert get_subject_token(state, "conv_1") is None


def test_blank_header_stashes_nothing():
    state = _state()
    stash_subject_token_from_headers(state, "conv_1", {SUBJECT_TOKEN_HEADER: "   "})
    assert get_subject_token(state, "conv_1") is None


def test_get_unknown_session_is_none():
    assert get_subject_token(_state(), "never-seen") is None


# ── bounded: TTL + maxsize ───────────────────────────────────────────────────


def test_stash_is_bounded_by_maxsize():
    from omnigent.server.subject_token_stash import _MAXSIZE, _get_or_create_stash

    state = _state()
    # Insert maxsize+50 distinct sessions; the cache must cap at maxsize.
    for i in range(_MAXSIZE + 50):
        stash_subject_token_from_headers(state, f"conv_{i}", {SUBJECT_TOKEN_HEADER: f"tok{i}"})
    cache = _get_or_create_stash(state)
    assert len(cache) <= _MAXSIZE


def test_stash_has_a_finite_ttl():
    from omnigent.server.subject_token_stash import _get_or_create_stash

    state = _state()
    stash_subject_token_from_headers(state, "conv_1", {SUBJECT_TOKEN_HEADER: "tok"})
    cache = _get_or_create_stash(state)
    assert cache.ttl > 0


# ── eviction on delete ───────────────────────────────────────────────────────


def test_evict_removes_the_entry():
    state = _state()
    stash_subject_token_from_headers(state, "conv_1", {SUBJECT_TOKEN_HEADER: "tok"})
    evict_subject_token(state, "conv_1")
    assert get_subject_token(state, "conv_1") is None


def test_evict_unknown_session_is_noop():
    # Best-effort: deleting a session that never stashed must not raise.
    evict_subject_token(_state(), "never-seen")


# ── degrade-safe against a None / odd state ──────────────────────────────────


def test_get_tolerates_state_without_stash_attr():
    # A fresh state that never stashed anything returns None, no AttributeError.
    assert get_subject_token(_state(), "conv_1") is None
