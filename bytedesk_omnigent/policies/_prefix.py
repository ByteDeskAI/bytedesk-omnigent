"""Shared server-prefix canonicalizer for built-in policies.

The google / github policies recognize tools by their *canonical* name after
stripping an MCP server prefix (``mcp__google__`` / ``github__`` / …). The
per-provider prefix LISTS live with each policy; only the strip algorithm is
shared here.
"""

from __future__ import annotations

from collections.abc import Iterable


def strip_server_prefix(name: str, prefixes: Iterable[str]) -> str:
    """
    Strip the first matching server prefix to get the canonical tool name.

    Prefixes are tried in the order given; callers list them longest-first so a
    more specific prefix (``mcp__google__``) wins over a bare one (``google__``).

    :param name: Raw tool name, e.g. ``"mcp__google__drive_file_get"`` or
        ``"github__create_pull_request"``.
    :param prefixes: Prefixes to try, in priority order (longest-first).
    :returns: The canonical name with the first matching prefix removed, or
        *name* unchanged when no prefix matches.
    """
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name
