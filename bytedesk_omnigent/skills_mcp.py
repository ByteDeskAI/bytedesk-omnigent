"""STDIO MCP front for the skill-acquisition routes (BDP-2462, Epic BDP-2461).

The Skills Concierge agent (a ``claude-sdk`` built-in) drives skill discovery
and installation through this thin MCP server. Each tool proxies one
**authenticated** HTTP call to an existing ``/v1/skills/*`` route (served by
``omnigent/server/routes/skills.py`` over ``SkillAcquisitionService``) or to
``/v1/agents`` for scope resolution — no skill logic is duplicated here.

Auth seam (proven against the live host pod): unlike the headerless
``memory_mcp`` front, ``/v1/skills/*`` is ``require_user``-gated, so this front
mints a bearer the same way the ``omnigent-configure-agent`` updater scripts do
— ``POST {base}/auth/login`` with the host credentials already present in the
runner env (``OMNIGENT_HOST_AUTH_USERNAME`` / ``OMNIGENT_HOST_AUTH_PASSWORD``),
caches the token, and re-logs-in once on a 401. This front is attached to the
Concierge agent ONLY (its spec ``mcp_servers``), never ``default_mcp_servers``.

Base URL: ``OMNIGENT_SELF_BASE_URL`` → ``OMNIGENT_SERVER_URL`` → in-cluster
default (mirrors ``memory_mcp``).
"""

from __future__ import annotations

import os
import threading
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

_DEFAULT_BASE_URL = "http://omnigent-server.bytedesk.svc.cluster.local"
_HTTP_TIMEOUT_S = 60.0

_token_lock = threading.Lock()
_cached_token: str | None = None


def _base_url() -> str:
    """Resolve the omnigent server base URL (no trailing slash)."""
    raw = (
        os.environ.get("OMNIGENT_SELF_BASE_URL")
        or os.environ.get("OMNIGENT_SERVER_URL")
        or _DEFAULT_BASE_URL
    )
    return raw.rstrip("/")


def _reset_token_cache() -> None:
    """Drop the cached bearer (test seam; also used after a 401)."""
    global _cached_token
    with _token_lock:
        _cached_token = None


def _raw(method: str, url: str, headers: dict[str, str], json: Any) -> tuple[int, Any]:
    """Low-level transport seam: one HTTP request → ``(status_code, json|None)``.

    Isolated so tests can substitute a fake transport without httpx. JSON decode
    failures surface as ``None`` body alongside the real status code.
    """
    with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.request(method, url, headers=headers, json=json)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _try_login() -> str | None:
    """Mint a bearer from host credentials, or ``None`` when none are present.

    Host credentials only exist on the host pod / admin path; the **runner**
    (where the real agent executes) deliberately holds no server credential, so
    this returns ``None`` there. Read-only skill routes (search/sources) are
    open to anonymous callers, so a ``None`` here is fine for those — only the
    user-gated routes (installed/preview/apply/remove) then surface a 401.
    """
    username = os.environ.get("OMNIGENT_HOST_AUTH_USERNAME")
    password = os.environ.get("OMNIGENT_HOST_AUTH_PASSWORD")
    if not username or not password:
        return None
    status, body = _raw(
        "POST",
        f"{_base_url()}/auth/login",
        {"content-type": "application/json"},
        {"username": username, "password": password},
    )
    token = body.get("token") if isinstance(body, dict) else None
    if status != 200 or not isinstance(token, str) or not token:
        return None
    return token


def _headers(token: str | None) -> dict[str, str]:
    h = {"content-type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Call ``{base}{path}`` — anonymous first, authenticate only if required.

    The read-only skill routes accept anonymous service callers, so try
    headerless first. On a 401 (a user-gated route), mint a bearer from host
    creds and retry — but in the runner no creds exist, so the 401 is surfaced
    as a clear, actionable error instead of crashing.

    :param path: A server-absolute path beginning with ``/``.
    :returns: The decoded JSON body.
    :raises RuntimeError: on a non-2xx response.
    """
    global _cached_token
    url = f"{_base_url()}{path}"
    status, payload = _raw(method, url, _headers(_cached_token), body)
    if status == 401:
        with _token_lock:
            _cached_token = _try_login()
        if _cached_token is not None:
            status, payload = _raw(method, url, _headers(_cached_token), body)
    if status == 401:
        raise RuntimeError(
            f"{method} {path} is not available to the agent: it requires an "
            "authenticated user, but the runner holds no server credential. "
            "Read-only discovery (search/sources) works anonymously; "
            "installing/removing skills needs the identity fix (BDP-2487)."
        )
    if not (200 <= status < 300):
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise RuntimeError(f"{method} {path} failed (status {status}): {detail}")
    return payload


# --- scope resolution helpers ----------------------------------------------


def _scope_matches(agent: dict[str, Any], scope: str) -> bool:
    """Whether *agent* falls within a scope phrase.

    Scope grammar: ``organization`` (all non-workflow agents),
    ``department:<name>``, ``employee:<id-or-name>``, or a bare agent
    id / display name. Workflow/orchestrator agents are excluded from
    ``organization`` / ``department`` by default (matching the
    omnigent-configure-agent contract).
    """
    if agent.get("workflow") is True and scope.startswith(("organization", "department")):
        return False
    if scope == "organization":
        return True
    if scope.startswith("department:"):
        want = scope[len("department:") :].strip().lower()
        return str(agent.get("department") or "").strip().lower() == want
    target = scope[len("employee:") :] if scope.startswith("employee:") else scope
    target = target.strip().lower()
    return target in (
        str(agent.get("id") or "").lower(),
        str(agent.get("name") or "").lower(),
        str(agent.get("display_name") or "").lower(),
    )


# --- MCP tools --------------------------------------------------------------

mcp = FastMCP("skills")


@mcp.tool()
def search(query: str, sources: list[str] | None = None, limit: int = 20) -> dict:
    """Search for installable agent skills online (skills.sh registry + npm).

    Returns ranked hits as ``{"results": [...], "errors": [...]}`` where each
    result has ``name`` (the ``owner/repo@skill`` install ref), ``source``,
    ``source_ref`` (pass this to ``stage_preview``), ``description``, ``url``.
    """
    body: dict[str, Any] = {"query": query, "limit": limit}
    if sources is not None:
        body["sources"] = sources
    out = _request("POST", "/v1/skills/search", body)
    return {"results": out.get("data", []), "errors": out.get("errors", [])}


@mcp.tool()
def sources() -> dict:
    """List the available skill sources and whether each is currently usable."""
    out = _request("GET", "/v1/skills/sources")
    return {"sources": out.get("data", [])}


@mcp.tool()
def installed(agent_id: str | None = None) -> dict:
    """List skills already installed (optionally for one agent)."""
    path = "/v1/skills/installed" + (f"?agent_id={agent_id}" if agent_id else "")
    out = _request("GET", path)
    return {"installed": out.get("data", [])}


@mcp.tool()
def resolve_targets(scope: str) -> dict:
    """Resolve a scope phrase to the concrete target agents to install into.

    ``scope`` is ``organization`` | ``department:<name>`` | ``employee:<id>`` |
    an agent display name. Workflow/orchestrator agents are excluded from
    org/department scopes. Returns ``{"targets": [{"id","display_name",
    "department"}]}`` — pass the ids to ``stage_preview``.
    """
    out = _request("GET", "/v1/agents?limit=1000&order=asc")
    targets = [
        {
            "id": a.get("id"),
            "display_name": a.get("display_name"),
            "department": a.get("department"),
        }
        for a in out.get("data", [])
        if _scope_matches(a, scope)
    ]
    return {"targets": targets}


@mcp.tool()
def stage_preview(
    source: str,
    source_ref: str,
    target_agent_ids: list[str],
    install_mode: str = "skip_existing",
    selected_skill_names: list[str] | None = None,
) -> dict:
    """Stage (but do NOT apply) an install: fetch + validate the skill files and
    compute the per-agent actions, returning a preview to confirm before apply.

    ``install_mode`` defaults to ``skip_existing`` so a re-run on an
    already-installed target is an idempotent no-op; use ``replace`` only for an
    explicit reinstall. ``selected_skill_names`` can stage many skills from a
    multi-skill source in one preview. Returns
    ``{"preview_id", "skills", "target_actions"}``.
    """
    body = {
        "operation": "install",
        "target_agent_ids": target_agent_ids,
        "install_mode": install_mode,
        "source": source,
        "source_ref": source_ref,
    }
    if selected_skill_names:
        body["selected_skill_names"] = selected_skill_names
    out = _request("POST", "/v1/skills/previews", body)
    return {
        "preview_id": out.get("id"),
        "skills": out.get("skills", []),
        "target_actions": out.get("target_actions", []),
    }


@mcp.tool()
def apply_preview(preview_id: str, agent_ids: list[str] | None = None) -> dict:
    """Apply a staged preview, persisting the skill into each target's bundle.

    ``agent_ids`` optionally narrows the apply to a subset of the preview's
    targets. Returns ``{"results": [{"agent_id","status","version","error"}]}``.
    """
    body: dict[str, Any] = {}
    if agent_ids is not None:
        body["target_agent_ids"] = agent_ids
    out = _request("POST", f"/v1/skills/previews/{preview_id}/apply", body)
    return {"results": out.get("data", [])}


@mcp.tool()
def remove(skill_name: str, target_agent_ids: list[str]) -> dict:
    """Uninstall a skill from the given targets — the rollback primitive.

    Stages a ``remove`` preview for ``skill_name`` then applies it. Returns
    ``{"results": [...]}`` (same shape as ``apply_preview``).
    """
    preview = _request(
        "POST",
        "/v1/skills/previews",
        {
            "operation": "remove",
            "target_agent_ids": target_agent_ids,
            "skill_names": [skill_name],
        },
    )
    out = _request(
        "POST",
        f"/v1/skills/previews/{preview.get('id')}/apply",
        {"target_agent_ids": target_agent_ids},
    )
    return {"results": out.get("data", [])}


def main() -> None:
    """Run the stdio MCP server (``python -m bytedesk_omnigent.skills_mcp``)."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
