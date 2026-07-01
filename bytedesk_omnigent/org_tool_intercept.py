"""Server-side execution for org source-of-truth MCP tools."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from bytedesk_omnigent.connectors.store import get_connector_store
from bytedesk_omnigent.workforce import effective_workforce_for_agent, get_workforce_store, slug
from omnigent.server.agent_refs import resolve_agent_ref

_logger = logging.getLogger(__name__)

_ORG_PREFIX = "org__"
_TOOLS = frozenset({"get_chart", "find_agent", "get_effective_access"})
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50


def is_org_tool(namespaced_name: str) -> bool:
    """Whether *namespaced_name* is one of the server-handled org tools."""
    if not namespaced_name.startswith(_ORG_PREFIX):
        return False
    return namespaced_name[len(_ORG_PREFIX) :] in _TOOLS


def execute_org_tool(
    namespaced_name: str,
    arguments: dict[str, Any] | None,
    *,
    caller_agent_id: str | None,
    caller_department: str | None = None,
) -> str:
    """Execute an ``org__*`` tool server-side and return a JSON result string."""
    if not is_org_tool(namespaced_name):
        return json.dumps({"ok": False, "error": "unknown_org_tool"})
    op = namespaced_name[len(_ORG_PREFIX) :]
    try:
        result = _dispatch(
            op,
            arguments or {},
            caller_agent_id=caller_agent_id,
            caller_department=caller_department,
        )
    except Exception:  # noqa: BLE001 - tool calls surface errors to the model.
        _logger.warning("org tool %r failed", namespaced_name, exc_info=True)
        result = {"ok": False, "error": "org_tool_failed"}
    return json.dumps(result)


def _dispatch(
    op: str,
    args: dict[str, Any],
    *,
    caller_agent_id: str | None,
    caller_department: str | None,
) -> dict[str, Any]:
    del caller_department
    if op == "get_chart":
        return _get_chart(args)
    if op == "find_agent":
        return _find_agent(args)
    if op == "get_effective_access":
        return _get_effective_access(args, caller_agent_id=caller_agent_id)
    return {"ok": False, "error": "unknown_org_tool"}


def _runtime_stores() -> tuple[Any, Any]:
    from omnigent.runtime import get_agent_cache, get_agent_store

    return get_agent_store(), get_agent_cache()


def _agent_rows(
    *,
    include_system: bool = False,
    include_harness: bool = False,
    include_workflow: bool = False,
) -> list[dict[str, Any]]:
    agent_store, agent_cache = _runtime_stores()
    include_categories = {"employee"}
    if include_system:
        include_categories.add("system")
    if include_harness:
        include_categories.add("harness")
    if include_workflow:
        include_categories.add("workflow")

    rows: list[dict[str, Any]] = []
    page = agent_store.list(limit=1000, order="asc")
    for agent in page.data:
        if agent.category not in include_categories:
            continue
        params: dict[str, Any] = {}
        try:
            loaded = agent_cache.load(
                agent.id,
                agent.bundle_location,
                expand_env=agent.session_id is None,
            )
            if isinstance(loaded.spec.params, dict):
                params = loaded.spec.params
        except Exception:  # noqa: BLE001 - broken bundles should not hide the roster.
            _logger.debug("failed to load org metadata for agent %s", agent.id, exc_info=True)
        department = _str_or_none(params.get("department"))
        display_name = _str_or_none(params.get("displayName"))
        title = _str_or_none(params.get("title"))
        managers = [m for m in params.get("managers", []) if isinstance(m, dict)]
        rows.append(
            {
                "agentId": agent.id,
                "name": agent.name,
                "displayName": display_name,
                "label": display_name or agent.name,
                "category": agent.category,
                "department": department,
                "departmentSlug": slug(department),
                "title": title,
                "managers": managers,
                "capabilities": list(_capabilities(agent_store, agent.id)),
            }
        )
    return sorted(rows, key=_agent_sort_key)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _capabilities(agent_store: Any, agent_id: str) -> tuple[str, ...]:
    try:
        return tuple(agent_store.get_capabilities(agent_id))
    except Exception:  # noqa: BLE001 - capability enrichment is optional.
        return ()


def _agent_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("department") or "").lower(), str(row.get("label") or "").lower())


def _include_args(args: dict[str, Any]) -> dict[str, bool]:
    return {
        "include_system": bool(args.get("include_system", False)),
        "include_harness": bool(args.get("include_harness", False)),
        "include_workflow": bool(
            args.get("include_workflow", args.get("include_workflows", False))
        ),
    }


def _get_chart(args: dict[str, Any]) -> dict[str, Any]:
    department_filter = slug(args.get("department")) if args.get("department") else None
    rows = _agent_rows(**_include_args(args))
    if department_filter:
        rows = [row for row in rows if row["departmentSlug"] == department_filter]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("department") or "Unassigned"].append(row)

    departments = [
        {
            "department": department,
            "departmentSlug": slug(department),
            "agents": sorted(agents, key=lambda row: str(row.get("label") or "").lower()),
        }
        for department, agents in grouped.items()
    ]
    departments.sort(key=lambda row: str(row["department"]).lower())
    counts = {
        "employees": sum(1 for row in rows if row["category"] == "employee"),
        "system": sum(1 for row in rows if row["category"] == "system"),
        "harness": sum(1 for row in rows if row["category"] == "harness"),
        "workflow": sum(1 for row in rows if row["category"] == "workflow"),
    }
    return {"ok": True, "counts": counts, "departments": departments, "agents": rows}


def _find_agent(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip().lower()
    if not query:
        return {"ok": False, "error": "query_required", "matches": []}
    category = str(args.get("category") or "employee").strip().lower()
    include = {
        "include_system": category in {"all", "system"},
        "include_harness": category in {"all", "harness"},
        "include_workflow": category in {"all", "workflow"},
    }
    department_filter = slug(args.get("department")) if args.get("department") else None
    rows = _agent_rows(**include)
    if category not in {"all", "employee", "system", "harness", "workflow"}:
        return {"ok": False, "error": "unsupported_category", "matches": []}
    if category != "all":
        rows = [row for row in rows if row["category"] == category]
    if department_filter:
        rows = [row for row in rows if row["departmentSlug"] == department_filter]

    matches = [row for row in rows if _matches(row, query)]
    limit = _limit(args.get("limit"))
    return {"ok": True, "matches": matches[:limit]}


def _matches(row: dict[str, Any], query: str) -> bool:
    fields = [
        row.get("agentId"),
        row.get("name"),
        row.get("displayName"),
        row.get("department"),
        row.get("title"),
        " ".join(row.get("capabilities") or []),
    ]
    haystack = " ".join(str(item).lower() for item in fields if item)
    return query in haystack


def _limit(raw: Any) -> int:
    try:
        return max(1, min(int(raw or _DEFAULT_LIMIT), _MAX_LIMIT))
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _normalize_agent_ref(agent_ref: str) -> str | None:
    ref = agent_ref.strip()
    if not ref:
        return None
    try:
        agent_store, _agent_cache = _runtime_stores()
        agent = resolve_agent_ref(agent_store, ref, template_only=True)
    except RuntimeError:
        return ref if ref.startswith("ag_") else None
    if agent is not None:
        return agent.id
    return ref if ref.startswith("ag_") else None


def _get_effective_access(args: dict[str, Any], *, caller_agent_id: str | None) -> dict[str, Any]:
    supplied_agent_id = args.get("agent_id") or args.get("agentId")
    if supplied_agent_id is not None:
        agent_id = _normalize_agent_ref(str(supplied_agent_id))
    else:
        agent_id = str(caller_agent_id or "").strip() or None
    if not agent_id:
        return {"ok": False, "error": "agent_identity_required"}
    workforce = effective_workforce_for_agent(agent_id, store=get_workforce_store())
    if workforce.get("found") is False:
        return {"ok": False, "error": "agent_not_found", "agentId": agent_id}

    result: dict[str, Any] = {"ok": True, "agentId": agent_id, "workforce": workforce}
    include_direct = bool(args.get("include_direct_grants", True))
    if include_direct:
        grants = get_connector_store().list_agent_grants(agent_id=agent_id)
        result["directConnectorGrantSummary"] = {
            "total": len(grants),
            "active": sum(1 for grant in grants if grant.enabled and grant.status == "active"),
            "disabled": sum(
                1 for grant in grants if not grant.enabled or grant.status != "active"
            ),
        }
        result["directConnectorGrants"] = [grant.to_dict() for grant in grants]
    return result


__all__ = ["execute_org_tool", "is_org_tool"]
