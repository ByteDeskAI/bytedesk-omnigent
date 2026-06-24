"""Per-session stash of the inbound ``X-Bytedesk-Subject-Token`` (BDP-2434 Part 1).

Office sends the user's outbound MCP access token on the **inbound** session
routes (``POST /v1/sessions`` and ``POST /v1/sessions/{id}/events``). The
acting-identity carrier that threads identity to the runner is minted later, on
the ``tools/call`` server→runner proxy hop, which does **not** carry that header.
So the inbound routes capture the token here, keyed by ``session_id``, and the
mint site (:func:`sessions._mint_acting_identity_header`) reads it back to fold
it into the carrier (BDP-2434 Part 2 codec).

The store is a bounded :class:`cachetools.TTLCache` on ``app.state`` so it can
never grow without limit and a stale token self-expires. The token value is
sensitive: it is **never logged** and only ever flows into the signed carrier.

Degrade-to-default: an absent/blank header stashes nothing, and a ``get`` for a
session that never stashed (or a state with no stash yet) returns ``None`` — so
the OBO path is dormant until Office actually sends the header.
"""

from __future__ import annotations

from typing import Any

from cachetools import TTLCache

#: Inbound header Office sets with the user's MCP access token (the Office contract).
SUBJECT_TOKEN_HEADER = "X-Bytedesk-Subject-Token"

#: ``app.state`` attribute holding the bounded per-session token cache.
_STASH_ATTR = "subject_token_stash"

#: Cap the cache so a burst of sessions can't grow it unbounded; each value is a
#: single short string, so this is generous.
_MAXSIZE = 4096

#: A subject_token is only needed for the lifetime of a turn's tool calls; a
#: 30-minute TTL outlives a long turn while bounding how long a token lingers.
_TTL_S = 1800.0


def _get_or_create_stash(state: Any) -> TTLCache:
    """Return the per-session stash on *state*, lazily creating it once.

    Lazy creation keeps the wiring minimal (no required server-build edit) and
    makes the helpers usable from focused tests with a bare state object.
    """
    cache = getattr(state, _STASH_ATTR, None)
    if not isinstance(cache, TTLCache):
        cache = TTLCache(maxsize=_MAXSIZE, ttl=_TTL_S)
        setattr(state, _STASH_ATTR, cache)
    return cache


def _read_header(headers: Any) -> str | None:
    """Read the subject-token header value (case-insensitive), or ``None``.

    Starlette's ``request.headers`` is already case-insensitive; a plain dict
    (tests) is checked for both the canonical and the lower-case key.
    """
    value = headers.get(SUBJECT_TOKEN_HEADER)
    if value is None:
        value = headers.get(SUBJECT_TOKEN_HEADER.lower())
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def stash_subject_token_from_headers(state: Any, session_id: str, headers: Any) -> None:
    """Capture the subject-token header for *session_id*, if present.

    No-op when the header is absent or blank (degrade-to-default — the OBO path
    stays dormant). Never logs the value.
    """
    token = _read_header(headers)
    if token is None:
        return
    _get_or_create_stash(state)[session_id] = token


def get_subject_token(state: Any, session_id: str) -> str | None:
    """Return the stashed subject_token for *session_id*, or ``None``.

    ``None`` for an unknown session, an expired entry, or a state that never
    stashed anything — the mint site degrades to today's behaviour on ``None``.
    """
    cache = getattr(state, _STASH_ATTR, None)
    if not isinstance(cache, TTLCache):
        return None
    return cache.get(session_id)


def evict_subject_token(state: Any, session_id: str) -> None:
    """Drop *session_id*'s stashed token (called on session delete). Best-effort."""
    cache = getattr(state, _STASH_ATTR, None)
    if isinstance(cache, TTLCache):
        cache.pop(session_id, None)


__all__ = [
    "SUBJECT_TOKEN_HEADER",
    "evict_subject_token",
    "get_subject_token",
    "stash_subject_token_from_headers",
]
