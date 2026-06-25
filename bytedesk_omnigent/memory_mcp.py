"""STDIO MCP front for the shared-memory route (BDP-2457 F1, amends ADR-0132).

The agent-memory plane (ADR-0132) is served as REST handlers on the omnigent
server (``bytedesk_omnigent/routes/memory.py``), but the LLM only reaches tools
through **MCP**. This module is the thin MCP front: a stdio MCP server the runner
spawns as the agent's ``mcp_servers`` command (``python -m
bytedesk_omnigent.memory_mcp``). Each tool proxies one HTTP call to the matching
``/v1/memory/...`` route and returns its JSON.

Why stdio (not an in-process ``app.mount`` of a streamable-HTTP MCP app): the
extension ``routers()`` seam only mounts ``APIRouter``s, so an ASGI MCP sub-app
would need an upstream ``omnigent/server/app.py`` edit. The stdio transport rides
the **proven stdio-MCP spawn path** (``omnigent/tools/mcp.py`` / the runner's
mcp_manager) with zero upstream edits — the architect-named fallback.

The model sees ``memory__recall`` / ``memory__append`` / ``memory__compartments``
— the server name is ``memory`` and the runner namespaces ``{server}__{tool}``.
Owner is stamped server-side by the route from the scope; this front never sends
an owner (the anti-spoof invariant). ``agent`` (private) scope is fail-closed at
the route pending per-agent identity on MCP egress; ``team`` / ``topic`` ship now.

Base URL resolution (the stdio subprocess runs on the HOST pod, which carries
``OMNIGENT_SERVER_URL``, not the server-only ``OMNIGENT_SELF_BASE_URL``): prefer
``OMNIGENT_SELF_BASE_URL`` → ``OMNIGENT_SERVER_URL`` → the in-cluster default
``http://omnigent-server.bytedesk.svc.cluster.local``. The agent config may also
pin it via the ``mcp_servers`` ``env:`` block.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

#: In-cluster default when neither env var is set (matches host.yaml / server.yaml).
_DEFAULT_BASE_URL = "http://omnigent-server.bytedesk.svc.cluster.local"

#: Per-call timeout. The route handlers are local DB reads/writes — sub-second.
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
    """POST *body* to ``{base}/v1/memory/{path}`` and return the JSON dict.

    Identity headers are intentionally NOT forwarded: ``team`` / ``topic`` are
    shared data and the live server is header/single-user
    (``OMNIGENT_AUTH_ENABLED=0``), so the call reaches the route as ``"local"``.
    """
    url = f"{_base_url()}/v1/memory/{path}"
    with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.post(url, json=body)
    # Surface the route's structured error body (e.g. the agent-scope fail-closed
    # message) to the model rather than raising — the JSON carries an "error" the
    # LLM can act on.
    try:
        return resp.json()
    except ValueError:
        return {"error": f"memory route {url} returned non-JSON ({resp.status_code})"}


mcp = FastMCP("memory")


@mcp.tool()
def recall(query: str, scope: str = "team", name: str = "org-context", limit: int = 10) -> dict:
    """Recall durable SHARED memories from a compartment, ranked by salience with
    stale memories decayed out. Check this BEFORE you decide or start work —
    recall the org blackboard (the default team/org-context) and any relevant
    department (topic/dept:<id>) or initiative compartment so you don't repeat or
    contradict what another agent already recorded.

    scope/name conventions: org-wide = scope='team', name='org-context' (the
    standing blackboard, the default); department = scope='topic',
    name='dept:<id>' (e.g. 'dept:engineering'); initiative = scope='topic',
    name='initiative:<id>'. (scope='agent' private memory is currently disabled.)
    """
    return _post("recall", {"query": query, "scope": scope, "name": name, "limit": limit})


@mcp.tool()
def append(
    content: str, scope: str = "team", name: str = "org-context", weight: float = 1.0
) -> dict:
    """Save a durable SHARED memory you can recall later. After you make a
    decision or learn something other agents should know, save it here so the
    whole org sees it — this is how agents share knowledge across sessions and
    teammates. Use for decisions, facts, preferences, and outcomes. Memories
    carry a weight and decay over time.

    scope/name conventions: org-wide = scope='team', name='org-context' (default);
    department = scope='topic', name='dept:<id>'; initiative = scope='topic',
    name='initiative:<id>'. (scope='agent' private memory is currently disabled.)
    """
    return _post("append", {"content": content, "scope": scope, "name": name, "weight": weight})


@mcp.tool()
def compartments() -> dict:
    """List your reachable SHARED memory compartments (team + topic), with the
    standing team/org-context blackboard always present."""
    return _post("compartments", {})


# ── addressable (keyed) memory: deterministic exact-key slots ─────────────────
#
# Unlike recall (fuzzy, decaying), an ADDRESS names one durable slot retrieved
# exactly. Put the address in your prompt and go straight to it. Address grammar:
# 'org:<key>' (org-wide), 'dept:<dept>:<key>' (department). Keys cannot contain
# ':'. ('agent:<id>:<key>' private slots are currently disabled.)


@mcp.tool()
def memory_get(address: str) -> dict:
    """Read the durable value at an exact memory ADDRESS, e.g. 'org:charter' or
    'dept:engineering:oncall'. Deterministic — always returns THIS slot, not a
    fuzzy match, and never decays. Use when your instructions reference a known
    address. Returns {"found": false} if the slot was never set."""
    return _post("get", {"address": address})


@mcp.tool()
def memory_put(
    address: str,
    content: str,
    weight: float = 1.0,
    confidence: float | None = None,
    source_conversation_id: str | None = None,
) -> dict:
    """Write the durable value at an exact memory ADDRESS, e.g. 'org:charter'.
    OVERWRITES any current value at that address (one current value per slot).
    Use for stable, prompt-referenced facts (charter, this-week focus, an oncall
    rota) that must be retrieved exactly, not fuzzily. Address grammar:
    'org:<key>' or 'dept:<dept>:<key>'."""
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
def memory_unset(address: str) -> dict:
    """Clear the durable value at an exact memory ADDRESS, e.g. 'org:charter'.
    Use to retire a slot a prompt no longer references."""
    return _post("unset", {"address": address})


def main() -> None:
    """Run the stdio MCP server (the ``python -m bytedesk_omnigent.memory_mcp`` entry)."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
