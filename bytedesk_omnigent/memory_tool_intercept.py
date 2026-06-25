"""Server-side execution of the three-tier memory tools (BDP-2458, ADR-0132).

The ``memory__*`` tools are handled by the omnigent server itself, at the
``tools/call`` choke point, instead of being dispatched to the runner's stdio MCP
front. Two reasons:

* **Identity.** The server already holds the SERVER-VERIFIED caller (the session's
  ``agent_id``) and its department (the agent bundle's ``spec.params.department``)
  at dispatch time. The stdio memory front is a single subprocess SHARED across
  agents with no per-call identity channel, so it physically cannot carry a
  trustworthy per-agent identity (the BDP-2458 blocker). Handling memory
  server-side sidesteps the transport entirely — owner is stamped from the
  verified identity, never the model (anti-spoof, ADR-0132/0133/0136).
* **It's local.** The memory store lives in this process; a runner round-trip back
  out to an HTTP route would be pure overhead.

Access is decided by :mod:`bytedesk_omnigent.memory_access` (org = everyone,
dept = members, agent = private). This module is just the glue: resolve access →
call the pluggable provider/store → return the tool's JSON result string. It is the
SOLE memory execution path — the old per-call ``/v1/memory`` HTTP route + its
httpx-proxy stdio front were removed once this interceptor covered every
``memory__*`` call; the stdio front now only advertises the tool schemas.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bytedesk_omnigent.memory_access import (
    AccessDenied,
    MemoryTarget,
    resolve_address,
    resolve_prefix,
    resolve_scope_name,
)

_logger = logging.getLogger(__name__)

_MEMORY_PREFIX = "memory__"
_TOOLS = frozenset({"search", "append", "put", "get", "list", "unset"})


def is_memory_tool(namespaced_name: str) -> bool:
    """Whether *namespaced_name* is one of the server-handled memory tools."""
    if not namespaced_name.startswith(_MEMORY_PREFIX):
        return False
    return namespaced_name[len(_MEMORY_PREFIX) :] in _TOOLS


def execute_memory_tool(
    namespaced_name: str,
    arguments: dict[str, Any] | None,
    *,
    caller_agent_id: str | None,
    caller_department: str | None,
) -> str:
    """Execute a ``memory__*`` tool server-side; return its JSON result string.

    :param namespaced_name: e.g. ``"memory__get"``.
    :param arguments: the model-supplied tool arguments (content is data; any
        scope/address is access-checked against the verified identity).
    :param caller_agent_id: the SERVER-VERIFIED calling agent id (session agent).
    :param caller_department: the caller's department (bundle ``spec.params``),
        or ``None``.
    :returns: a JSON string (a result dict, or ``{"error": ...}``).
    """
    op = namespaced_name[len(_MEMORY_PREFIX) :]
    args = arguments or {}
    try:
        result = _dispatch(
            op, args, caller_agent_id=caller_agent_id, caller_department=caller_department
        )
    except ValueError as exc:
        result = {"error": str(exc)}
    except Exception:  # noqa: BLE001 — never 500 the tool path; surface to the model.
        _logger.warning("memory tool %r failed", namespaced_name, exc_info=True)
        result = {"error": "memory tool failed"}
    return json.dumps(result)


def _dispatch(
    op: str,
    args: dict[str, Any],
    *,
    caller_agent_id: str | None,
    caller_department: str | None,
) -> dict[str, Any]:
    from omnigent.runtime import get_memory_provider, get_memory_store

    if op == "search":
        target = resolve_scope_name(
            args.get("scope", "team"),
            args.get("name", "org-context"),
            caller_agent_id=caller_agent_id,
            caller_department=caller_department,
        )
        if isinstance(target, AccessDenied):
            return {"error": target.reason}
        provider = get_memory_provider()
        hits = provider.recall(
            scope=target.scope,
            owner=target.owner,
            name=target.name,
            query=args.get("query", ""),
            k=int(args.get("limit", 10)),
            kind=args.get("kind", "all"),
        )
        provider.note_recalled(hits)
        results = [
            {"content": h.content, "weight": round(h.effective_weight, 4), "memory_id": h.id}
            for h in hits
        ]
        if not results:
            return {"results": [], "message": "No matching memories."}
        return {"results": results}

    if op == "append":
        target = resolve_scope_name(
            args.get("scope", "team"),
            args.get("name", "org-context"),
            caller_agent_id=caller_agent_id,
            caller_department=caller_department,
        )
        if isinstance(target, AccessDenied):
            return {"error": target.reason}
        memory_id = get_memory_provider().write(
            scope=target.scope,
            owner=target.owner,
            name=target.name,
            content=args.get("content", ""),
            weight=float(args.get("weight", 1.0)),
        )
        return {"memory_id": memory_id, "scope": target.scope, "compartment": target.name}

    if op == "put":
        target = _resolve_addr(args.get("address", ""), caller_agent_id, caller_department)
        if isinstance(target, AccessDenied):
            return {"error": target.reason}
        store = get_memory_store()
        replaced = store.archive_keyed(
            scope=target.scope, owner=target.owner, name=target.name, key=target.key
        )
        memory_id = get_memory_provider().write(
            scope=target.scope,
            owner=target.owner,
            name=target.name,
            content=args.get("content", ""),
            weight=float(args.get("weight", 1.0)),
            source_conversation_id=args.get("source_conversation_id"),
            confidence=args.get("confidence"),
            key=target.key,
        )
        return {"address": args.get("address"), "memory_id": memory_id, "overwrote": replaced}

    if op == "get":
        target = _resolve_addr(args.get("address", ""), caller_agent_id, caller_department)
        if isinstance(target, AccessDenied):
            return {"error": target.reason}
        slot = get_memory_store().get_keyed(
            scope=target.scope, owner=target.owner, name=target.name, key=target.key
        )
        if slot is None:
            return {"address": args.get("address"), "found": False}
        slot["weight"] = round(slot["weight"], 4)
        return {"address": args.get("address"), "found": True, **slot}

    if op == "unset":
        target = _resolve_addr(args.get("address", ""), caller_agent_id, caller_department)
        if isinstance(target, AccessDenied):
            return {"error": target.reason}
        cleared = get_memory_store().archive_keyed(
            scope=target.scope, owner=target.owner, name=target.name, key=target.key
        )
        if cleared == 0:
            return {"address": args.get("address"), "found": False}
        return {"address": args.get("address"), "cleared": cleared}

    if op == "list":
        target = resolve_prefix(
            args.get("prefix", "org"),
            caller_agent_id=caller_agent_id,
            caller_department=caller_department,
        )
        if isinstance(target, AccessDenied):
            return {"error": target.reason}
        slots = get_memory_store().list_keyed(
            scope=target.scope, owner=target.owner, name=target.name
        )
        return {"prefix": args.get("prefix", "org"), "slots": slots}

    return {"error": f"unknown memory tool {op!r}"}


def _resolve_addr(
    address: str, caller_agent_id: str | None, caller_department: str | None
) -> MemoryTarget | AccessDenied:
    return resolve_address(
        address, caller_agent_id=caller_agent_id, caller_department=caller_department
    )
