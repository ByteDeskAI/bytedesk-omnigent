"""HTTP ETag / If-Match parsing for optimistic concurrency (BDP-2412, ADR-0150).

omnigent's mutable rows use a monotonic integer ``version`` as their ETag. A
conditional write sends it back in the ``If-Match`` request header; the store's
guarded update compares-and-swaps on it. This module is the one place that turns
the wire header into the integer the store expects, so quoting/weak-validator
handling can't drift between routes.
"""

from __future__ import annotations

from omnigent.errors import ErrorCode, OmnigentError


def parse_if_match(header: str | None) -> int | None:
    """Parse an ``If-Match`` header into an expected ``version`` int, or ``None``.

    ``None`` means "no precondition" — the caller did not send ``If-Match`` and
    the store performs its unconditional update (back-compat). Handles optional
    double-quoting and the weak-validator ``W/`` prefix (RFC 7232). ``*`` ("any
    current representation") also returns ``None`` since existence is already
    enforced by the update's not-found path.

    :param header: Raw ``If-Match`` header value, or ``None`` if absent.
    :returns: The expected integer version, or ``None`` for no precondition.
    :raises OmnigentError: ``INVALID_INPUT`` (400) if a non-empty header is
        present but is not a valid integer ETag — failing closed rather than
        silently skipping the precondition.
    """
    if not header:
        return None
    token = header.strip()
    if token in ("", "*"):
        return None
    if token.startswith("W/"):
        token = token[2:].strip()
    token = token.strip('"').strip()
    if not token:
        return None
    try:
        return int(token)
    except ValueError as exc:
        raise OmnigentError(
            f"malformed If-Match ETag: {header!r} (expected an integer version)",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
