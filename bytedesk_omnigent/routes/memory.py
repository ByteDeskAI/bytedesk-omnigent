"""Agent-callable shared-memory route (BDP-2457, amends ADR-0132).

Exposes the omnigent-native agent-memory plane (ADR-0132) as HTTP endpoints an
agent reaches through a stdio **MCP** front (``bytedesk_omnigent/memory_mcp.py``)
— the only working agent-tool seam (the ``omnigent/tools/builtins/memory.py``
builtins are not schema-injected for agents, so they are dead for LLM turns).

Two families, both stamping ``owner`` server-side from the scope/address (never
agent-supplied — the anti-spoof invariant), and BOTH routing through the
pluggable port (``get_memory_provider()`` for similarity ops, ``get_memory_store()``
for the store-level keyed ops) — never a reach-through to ``db.utils`` / raw
``db_models`` from the route:

* **Ambient** (decaying, similarity-recalled): ``recall`` / ``append`` /
  ``compartments``. Scope → owner (identical to ``memory.py:_resolve_owner``):
  ``team`` → ``"team"`` (``name`` ``org-context`` = the standing blackboard);
  ``topic`` → ``"shared"`` (``name`` ``dept:<id>`` = department, ``initiative:<id>``
  = an initiative log).
* **Addressable** (deterministic, exact-key, no decay, excluded from similarity
  recall): ``get`` / ``put`` / ``unset`` over an ``address`` like ``org:charter``
  or ``dept:engineering:oncall``. Backed by the first-class ``memories.key``
  column + its partial-unique live-slot index — ``put`` overwrites in place
  (archive prior live slot, then ``append(key=…)``).

``agent`` (private) scope / ``agent:<id>:<key>`` addresses are **fail-closed**:
the private owner must be a server-VERIFIED agent id, and the runner does not
forward a verifiable per-agent identity onto its HTTP MCP egress today (identity
rides only an OBO bearer, which needs a user ``subject_token`` the autonomous/
Office turn path lacks). They return an error until a runner change forwards the
signed ``X-Omnigent-Acting-Identity`` carrier onto MCP egress (the follow-on).
``team`` / ``topic`` need no identity (constant owners) and ship now.

Auth: gated by ``require_user`` (like every sibling bytedesk route). Multi-user
mode rejects an unauthenticated caller (401); single-user mode
(``auth_provider=None``, and the live ``OMNIGENT_AUTH_ENABLED=0`` header/
single-user deployment) leaves it open, so the runner's MCP connection reaches
the handler as the reserved ``"local"`` identity — shared data is convention,
not a per-caller security boundary, so service-level auth is sufficient.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

# Scope → server-stamped owner. ``agent`` deliberately absent: it has no constant
# owner (it requires a verified per-agent identity), so it fails closed below.
_TEAM_OWNER = "team"
_TOPIC_OWNER = "shared"

#: The error returned for an ``agent`` (private) scope/address — fail-closed
#: until per-agent identity is wired onto MCP egress (see module docstring).
_AGENT_SCOPE_DISABLED = (
    "agent-scope (private) memory requires a verified per-agent identity, which "
    "is not yet wired onto MCP egress (see ADR amendment / follow-on). Use "
    "scope=team or scope=topic for shared data."
)


def _resolve_shared_owner(scope: str) -> str:
    """Return the server-derived owner for a SHARED *scope*.

    Mirrors ``memory.py:_resolve_owner`` for the team/topic cases; ``agent`` and
    any unknown scope raise so the caller fails closed.

    :param scope: One of ``team`` / ``topic``.
    :returns: The constant shared owner key.
    :raises ValueError: For ``agent`` (deferred) or any unknown scope.
    """
    if scope == "team":
        return _TEAM_OWNER
    if scope == "topic":
        return _TOPIC_OWNER
    if scope == "agent":
        raise ValueError(_AGENT_SCOPE_DISABLED)
    raise ValueError(f"invalid memory scope {scope!r}; expected 'team' or 'topic'")


def _parse_address(address: str) -> tuple[str, str, str, str]:
    """Parse an addressable-memory ``address`` into ``(scope, owner, name, key)``.

    The address is the prompt-friendly form (ADR-0132 addressable amendment):

    - ``org:<key>``            → team  / org-context  / ``<key>``
    - ``dept:<dept>:<key>``    → topic / dept:<dept>  / ``<key>``
    - ``agent:<id>:<key>``     → agent / <id>         → fails closed (deferred)

    The owner is server-stamped from the class (never the address) — the
    anti-spoof invariant. Keys may not contain ``:`` so the split is
    unambiguous: the last token is the key.

    :raises ValueError: malformed address, a key containing ``:``, or the
        ``agent`` (private) class which is deferred / fail-closed.
    """
    raw = (address or "").strip()
    if not raw:
        raise ValueError("address is required, e.g. 'org:charter' or 'dept:engineering:oncall'")
    parts = raw.split(":")
    cls = parts[0]
    if cls == "org":
        # org:<key>
        if len(parts) != 2 or not parts[1]:
            raise ValueError("org address must be 'org:<key>' (key may not contain ':')")
        return "team", _TEAM_OWNER, "org-context", parts[1]
    if cls == "dept":
        # dept:<dept>:<key>
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise ValueError(
                "dept address must be 'dept:<dept>:<key>' (neither may contain ':')"
            )
        return "topic", _TOPIC_OWNER, f"dept:{parts[1]}", parts[2]
    if cls == "agent":
        # agent:<id>:<key> — private scope, deferred / fail-closed.
        raise ValueError(_AGENT_SCOPE_DISABLED)
    raise ValueError(
        f"unknown address class {cls!r}; expected 'org:<key>', 'dept:<dept>:<key>', "
        "or 'agent:<id>:<key>'"
    )


def _parse_prefix(prefix: str) -> tuple[str, str, str]:
    """Parse a list *prefix* into ``(scope, owner, name)`` — ``org`` or ``dept:<dept>``.

    The browse-by-prefix counterpart to :func:`_parse_address` (no key). Owner is
    server-stamped from the class (anti-spoof). ``agent:`` is deferred/fail-closed.
    """
    raw = (prefix or "").strip()
    if raw == "org":
        return "team", _TEAM_OWNER, "org-context"
    if raw.startswith("dept:"):
        dept = raw[len("dept:") :]
        if not dept or ":" in dept:
            raise ValueError("dept prefix must be 'dept:<dept>' (no ':' in <dept>)")
        return "topic", _TOPIC_OWNER, f"dept:{dept}"
    if raw.startswith("agent:") or raw == "agent":
        raise ValueError(_AGENT_SCOPE_DISABLED)
    raise ValueError(f"unknown list prefix {raw!r}; expected 'org' or 'dept:<dept>'")


class _RecallBody(BaseModel):
    query: str
    scope: str = "team"
    name: str = "org-context"
    limit: int = 10
    # BDP-2459: which rows to search — "all" (ambient + addressable; the default
    # for unified search), "ambient", or "addressable".
    kind: str = "all"


class _AppendBody(BaseModel):
    content: str
    scope: str = "team"
    name: str = "org-context"
    weight: float = 1.0


class _CompartmentsBody(BaseModel):
    # No fields — the reachable shared compartments are server-derived. Present so
    # the MCP tool has a (trivial) object body like its sibling tools.
    pass


class _GetBody(BaseModel):
    address: str


class _PutBody(BaseModel):
    address: str
    content: str
    weight: float = 1.0
    confidence: float | None = None
    source_conversation_id: str | None = None


class _UnsetBody(BaseModel):
    address: str


class _ListBody(BaseModel):
    prefix: str = "org"


def create_memory_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the agent-callable shared-memory router.

    :param auth_provider: Auth provider used to identify the requesting caller.
        When set (multi-user mode) every handler requires a valid identity;
        ``None`` (single-user mode) leaves them open — the runner's MCP
        connection then reaches the handlers as the reserved ``"local"`` caller.
    """
    router = APIRouter()

    @router.post("/memory/recall")
    async def recall(request: Request, body: _RecallBody) -> JSONResponse:
        """Recall shared memories from a compartment, ranked by decayed salience."""
        require_user(request, auth_provider)
        try:
            owner = _resolve_shared_owner(body.scope)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        from omnigent.runtime import get_memory_provider

        provider = get_memory_provider()
        try:
            hits = provider.recall(
                scope=body.scope, owner=owner, name=body.name, query=body.query,
                k=int(body.limit), kind=body.kind,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        # Out-of-band reinforcement (off the recall path) — mirrors MemoryQueryTool.
        provider.note_recalled(hits)
        results = [
            {
                "content": hit.content,
                "weight": round(hit.effective_weight, 4),
                "memory_id": hit.id,
            }
            for hit in hits
        ]
        if not results:
            return JSONResponse({"results": [], "message": "No matching memories."})
        return JSONResponse({"results": results})

    @router.post("/memory/append")
    async def append(request: Request, body: _AppendBody) -> JSONResponse:
        """Save a durable shared memory into a compartment."""
        require_user(request, auth_provider)
        try:
            owner = _resolve_shared_owner(body.scope)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        from omnigent.runtime import get_memory_provider

        provider = get_memory_provider()
        try:
            memory_id = provider.write(
                scope=body.scope,
                owner=owner,
                name=body.name,
                content=body.content,
                weight=float(body.weight),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {"memory_id": memory_id, "scope": body.scope, "compartment": body.name}
        )

    @router.post("/memory/compartments")
    async def compartments(request: Request, body: _CompartmentsBody) -> JSONResponse:
        """List the reachable shared (team + topic) memory compartments."""
        del body
        require_user(request, auth_provider)
        from omnigent.memory_protocol import ensure_org_compartments
        from omnigent.runtime import get_memory_provider

        provider = get_memory_provider()
        comps: list[dict[str, Any]] = []
        comps += provider.list_compartments(scope="team")
        comps += provider.list_compartments(scope="topic")
        out = [{"scope": c["scope"], "name": c["name"]} for c in comps]
        # Always surface the standing org blackboard (team/org-context) even
        # before its first write — mirrors MemoryCompartmentsListTool.
        out = ensure_org_compartments(out)
        return JSONResponse({"compartments": out})

    # ── addressable (keyed) memory: exact-key get / put / unset ───────────────
    #
    # Deterministic slots keyed on the first-class ``memories.key`` column (no
    # similarity, no decay). All store access goes through the blessed
    # ``get_memory_store()`` accessor + ``provider.write(key=…)`` — the SQL lives
    # in ``SqlAlchemyMemoryStore.get_keyed`` / ``archive_keyed``, never the route.

    @router.post("/memory/get")
    async def get(request: Request, body: _GetBody) -> JSONResponse:
        """Exact-key read of an addressable slot (e.g. ``org:charter``).

        Deterministic lookup on the indexed ``key`` column — no similarity search,
        no decay. Returns the slot's current value or ``{"found": false}``.
        """
        require_user(request, auth_provider)
        try:
            scope, owner, name, key = _parse_address(body.address)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        from omnigent.runtime import get_memory_store

        slot = get_memory_store().get_keyed(scope=scope, owner=owner, name=name, key=key)
        if slot is None:
            return JSONResponse({"address": body.address, "found": False})
        slot["weight"] = round(slot["weight"], 4)
        return JSONResponse({"address": body.address, "found": True, **slot})

    @router.post("/memory/put")
    async def put(request: Request, body: _PutBody) -> JSONResponse:
        """Write an addressable slot, OVERWRITING any current value at this key.

        Overwrite-in-place: any live row(s) at the key are archived (history
        retained via the archive flag), then the new content is written as the
        single live slot on the first-class ``key`` column. Owner is
        server-stamped from the address class (anti-spoof).
        """
        require_user(request, auth_provider)
        try:
            scope, owner, name, key = _parse_address(body.address)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        from omnigent.runtime import get_memory_provider, get_memory_store

        replaced = get_memory_store().archive_keyed(
            scope=scope, owner=owner, name=name, key=key
        )
        try:
            memory_id = get_memory_provider().write(
                scope=scope,
                owner=owner,
                name=name,
                content=body.content,
                weight=float(body.weight),
                source_conversation_id=body.source_conversation_id,
                confidence=body.confidence,
                key=key,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {"address": body.address, "memory_id": memory_id, "overwrote": replaced}
        )

    @router.post("/memory/unset")
    async def unset(request: Request, body: _UnsetBody) -> JSONResponse:
        """Clear an addressable slot — archive whatever lives at this key."""
        require_user(request, auth_provider)
        try:
            scope, owner, name, key = _parse_address(body.address)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        from omnigent.runtime import get_memory_store

        cleared = get_memory_store().archive_keyed(
            scope=scope, owner=owner, name=name, key=key
        )
        if cleared == 0:
            return JSONResponse({"address": body.address, "found": False})
        return JSONResponse({"address": body.address, "cleared": cleared})

    @router.post("/memory/list")
    async def list_slots(request: Request, body: _ListBody) -> JSONResponse:
        """Browse addressable slots under a prefix (``org`` or ``dept:<dept>``).

        Query-less discovery of the keyed slots in a compartment — deterministic,
        newest first. The fuzzy counterpart is ``/memory/recall`` (kind=all).
        """
        require_user(request, auth_provider)
        try:
            scope, owner, name = _parse_prefix(body.prefix)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        from omnigent.runtime import get_memory_store

        slots = get_memory_store().list_keyed(scope=scope, owner=owner, name=name)
        return JSONResponse({"prefix": body.prefix, "slots": slots})

    return router
