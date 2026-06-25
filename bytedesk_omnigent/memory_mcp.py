"""STDIO MCP front that ADVERTISES the unified shared-memory tools (BDP-2457/2459/2458).

The agent-memory plane is executed SERVER-SIDE at the omnigent server's
``tools/call`` choke point (``_handle_mcp_tools_call`` →
:mod:`bytedesk_omnigent.memory_tool_intercept`), where the caller's VERIFIED
identity (``agent_id`` + department) is known, so the compartment owner is stamped
server-side and never from the model (the anti-spoof invariant, ADR-0132/0133/0136).

The LLM only discovers tools through **MCP**, and tool advertisement is runner-side,
so this stdio server exists ONLY to declare the ``memory__*`` tool schemas for the
runner to list. Its tool BODIES are never invoked — the server intercepts every
``memory__*`` call by name before it would reach the runner — so they are inert
stubs. (Removing this front entirely needs a server-side tool-advertisement seam;
tracked as the BDP-2458 follow-up. The old per-call ``/v1/memory`` HTTP route the
bodies used to proxy to was deleted with this slimming.)

One store, two write modes, one search:

* ``append`` — AMBIENT memory (decays); for observations/notes that accumulate.
* ``put`` / ``get`` / ``unset`` / ``list`` — ADDRESSABLE slots (keyed by an
  address like ``org:charter``; durable, overwrite-in-place, exact lookup).
* ``search`` — semantic + keyword search spanning **both** (kind = all / ambient
  / addressable).

Three tiers (BDP-2458): ``org:*`` every agent; ``dept:<id>:*`` members of that
department only; ``agent:*`` private to the caller. The model sees
``memory__search`` / ``memory__get`` / ``memory__put`` / ``memory__append`` /
``memory__list`` / ``memory__unset``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("memory")

# Tool bodies are advertisement-only: the omnigent server handles every
# ``memory__*`` call server-side (by tool name) before the runner would invoke
# this front, so the bodies are never reached. They return a clear sentinel
# purely as a tripwire if that invariant is ever broken.
_SERVER_SIDE_STUB = {
    "error": "memory tools execute server-side at the tools/call choke point "
    "(_handle_mcp_tools_call); this advertisement-only stub must not be invoked"
}


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
    org-wide = scope='team', name='org-context' (default, every agent); a
    department = scope='topic', name='dept:<id>' (only members of <id> see it);
    initiative = scope='topic', name='initiative:<id>'; your OWN private memory =
    scope='agent' (only you can read it).
    """
    del query, scope, name, kind, limit
    return dict(_SERVER_SIDE_STUB)


@mcp.tool()
def append(
    content: str, scope: str = "team", name: str = "org-context", weight: float = 1.0
) -> dict:
    """Save an AMBIENT shared memory (decays over time) — for observations, notes,
    and outcomes that should accumulate and fade. After a decision or learning
    other agents should know, save it so the whole org sees it (find it later with
    `search`). For a stable named fact you'll look up by address, use `put` instead.

    scope/name: org-wide = scope='team', name='org-context' (default, every agent);
    department = scope='topic', name='dept:<id>' (members of <id> only); initiative =
    scope='topic', name='initiative:<id>'; private to you = scope='agent'.
    """
    del content, scope, name, weight
    return dict(_SERVER_SIDE_STUB)


@mcp.tool()
def put(
    address: str,
    content: str,
    weight: float = 1.0,
    confidence: float | None = None,
    source_conversation_id: str | None = None,
) -> dict:
    """Write a durable ADDRESSABLE slot at an exact address (e.g. 'org:charter',
    'dept:engineering:oncall', 'agent:scratchpad'). OVERWRITES any current value
    (one per address), never decays, retrieved exactly by `get` and also found by
    `search`. Use for stable, prompt-referenced facts (charter, this-week focus, an
    oncall rota, a private note). Address grammar + who can read it: 'org:<key>' =
    every agent; 'dept:<dept>:<key>' = only members of that department; 'agent:<key>'
    = private to you (no other agent can read it)."""
    del address, content, weight, confidence, source_conversation_id
    return dict(_SERVER_SIDE_STUB)


@mcp.tool()
def get(address: str) -> dict:
    """Read the durable value at an exact ADDRESS (e.g. 'org:charter',
    'dept:engineering:oncall', 'agent:scratchpad'). Deterministic — always THIS slot,
    no fuzzy match, no decay. You may read org (any agent), your own department, and
    your own private 'agent:<key>' slots. Returns {"found": false} if unset."""
    del address
    return dict(_SERVER_SIDE_STUB)


@mcp.tool(name="list")
def list_slots(prefix: str = "org") -> dict:
    """List the addressable slots under a prefix ('org', 'dept:<id>', or 'agent' for
    your own private slots) WITHOUT a query — browse what's stored, newest first. The
    fuzzy, meaning-based counterpart is `search`."""
    del prefix
    return dict(_SERVER_SIDE_STUB)


@mcp.tool()
def unset(address: str) -> dict:
    """Clear (retire) the addressable slot at an exact ADDRESS, e.g. 'org:charter'.
    Use when a slot a prompt no longer references should be removed."""
    del address
    return dict(_SERVER_SIDE_STUB)


def main() -> None:
    """Run the stdio MCP server (the ``python -m bytedesk_omnigent.memory_mcp`` entry)."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
