"""STDIO MCP front for the unified shared-memory route (BDP-2457/2459, ADR-0132).

The agent-memory plane is served as REST handlers on the omnigent server
(``bytedesk_omnigent/routes/memory.py``), but the LLM only reaches tools through
**MCP**. This module is the thin MCP front: a stdio MCP server the runner spawns
as the agent's ``mcp_servers`` command (``python -m bytedesk_omnigent.memory_mcp``).
Each tool proxies one HTTP call to the matching ``/v1/memory/...`` route.

One store, two write modes, one search:

* ``append`` — AMBIENT memory (decays); for observations/notes that accumulate.
* ``put`` / ``get`` / ``unset`` / ``list`` — ADDRESSABLE slots (keyed by an
  address like ``org:charter``; durable, overwrite-in-place, exact lookup).
* ``search`` — semantic + keyword search spanning **both** (kind = all / ambient
  / addressable).

Owner is stamped server-side from the scope/address (never the model — the
anti-spoof invariant). ``agent``-private scope/addresses fail closed pending
per-agent identity on MCP egress (BDP-2458). The model sees ``memory__search`` /
``memory__get`` / ``memory__put`` / ``memory__append`` / ``memory__list`` /
``memory__unset``.

Base URL: ``OMNIGENT_SELF_BASE_URL`` → ``OMNIGENT_SERVER_URL`` (the host pod
carries it) → the in-cluster default.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

_DEFAULT_BASE_URL = "http://omnigent-server.bytedesk.svc.cluster.local"
_HTTP_TIMEOUT_S = 30.0


def _base_url() -> str:
    """Resolve the omnigent server base URL (no trailing slash)."""
    raw = (
        os.environ.get("OMNIGENT_SELF_BASE_URL")
        or os.environ.get("OMNIGENT_SERVER_URL")
        or _DEFAULT_BASE_URL
    )
    return raw.rstrip("/")


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST *body* to ``{base}/v1/memory/{path}`` and return the JSON dict."""
    url = f"{_base_url()}/v1/memory/{path}"
    with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.post(url, json=body)
    try:
        return resp.json()
    except ValueError:
        return {"error": f"memory route {url} returned non-JSON ({resp.status_code})"}


mcp = FastMCP("memory")


@mcp.tool()
def search(
    query: str,
    scope: str = "team",
    name: str = "org-context",
    kind: str = "all",
    limit: int = 10,
) -> dict:
    """Search SHARED memory by meaning or keyword — spans BOTH ambient (decaying)
    memories AND addressable slots. Check this before you decide or start work, so
    you don't repeat or contradict what a teammate already recorded.

    kind: 'all' (default) / 'ambient' / 'addressable'. scope/name conventions:
    org-wide = scope='team', name='org-context' (default); department =
    scope='topic', name='dept:<id>'; initiative = scope='topic',
    name='initiative:<id>'. (scope='agent' private memory is currently disabled.)
    """
    return _post(
        "recall", {"query": query, "scope": scope, "name": name, "kind": kind, "limit": limit}
    )


@mcp.tool()
def append(
    content: str, scope: str = "team", name: str = "org-context", weight: float = 1.0
) -> dict:
    """Save an AMBIENT shared memory (decays over time) — for observations, notes,
    and outcomes that should accumulate and fade. After a decision or learning
    other agents should know, save it so the whole org sees it (find it later with
    `search`). For a stable named fact you'll look up by address, use `put` instead.

    scope/name: org-wide = scope='team', name='org-context' (default); department =
    scope='topic', name='dept:<id>'; initiative = scope='topic', name='initiative:<id>'.
    """
    return _post("append", {"content": content, "scope": scope, "name": name, "weight": weight})


@mcp.tool()
def put(
    address: str,
    content: str,
    weight: float = 1.0,
    confidence: float | None = None,
    source_conversation_id: str | None = None,
) -> dict:
    """Write a durable ADDRESSABLE slot at an exact address (e.g. 'org:charter',
    'dept:engineering:oncall'). OVERWRITES any current value (one per address),
    never decays, retrieved exactly by `get` and also found by `search`. Use for
    stable, prompt-referenced facts (charter, this-week focus, an oncall rota).
    Address grammar: 'org:<key>' or 'dept:<dept>:<key>'."""
    return _post(
        "put",
        {
            "address": address,
            "content": content,
            "weight": weight,
            "confidence": confidence,
            "source_conversation_id": source_conversation_id,
        },
    )


@mcp.tool()
def get(address: str) -> dict:
    """Read the durable value at an exact ADDRESS (e.g. 'org:charter'). Deterministic
    — always THIS slot, no fuzzy match, no decay. Use when your instructions
    reference a known address. Returns {"found": false} if the slot was never set."""
    return _post("get", {"address": address})


@mcp.tool(name="list")
def list_slots(prefix: str = "org") -> dict:
    """List the addressable slots under a prefix ('org' or 'dept:<id>') WITHOUT a
    query — browse what's stored, newest first. The fuzzy, meaning-based counterpart
    is `search`."""
    return _post("list", {"prefix": prefix})


@mcp.tool()
def unset(address: str) -> dict:
    """Clear (retire) the addressable slot at an exact ADDRESS, e.g. 'org:charter'.
    Use when a slot a prompt no longer references should be removed."""
    return _post("unset", {"address": address})


def main() -> None:
    """Run the stdio MCP server (the ``python -m bytedesk_omnigent.memory_mcp`` entry)."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
