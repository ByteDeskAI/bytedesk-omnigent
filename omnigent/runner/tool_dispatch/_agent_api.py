"""Runner-local tool dispatch for intercepted action_required events.

Per designs/RUNNER_TOOL_DISPATCH.md, the runner dispatches most tools
locally and relays action_required events upstream UNCHANGED for
visibility (the executor emits ToolCallInProgress/ToolCallObserved for
the REPL but doesn't dispatch itself — it checks should_dispatch_locally
and skips).

Tool categories:
- _OS_ENV_TOOLS: execute through a runner-local OSEnvironment (sys_os_*)
- _REST_TOOLS: call server REST APIs (sys_call_async, sys_cancel_async)
- _FILE_TOOLS: call server file APIs (sys_upload/download/list_files)
- _TERMINAL_TOOLS: runner-local TerminalRegistry
- MCP tools: spec-defined; dispatched via RunnerMcpManager passed
  in by proxy_stream (designs/RUNNER_MCP.md). Not in the static
  allow-list because names vary per spec.
- Client-side tools: tunneled via REPL (deferred)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from omnigent.identity.identity import ActingIdentity
    from omnigent.runner.mcp_manager import McpManager
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runtime.filesystem_registry import FilesystemRegistry
    from omnigent.spec.types import AgentSpec
    from omnigent.terminals.registry import TerminalRegistry

import httpx

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
)
from omnigent.model_override import (
    harness_supports_model_override,
    model_family_mismatch,
    normalize_model_for_provider,
    validate_model_override,
)
from omnigent.runner.subagent_status import (
    _ACTIVE as _SUBAGENT_ACTIVE_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
)
from omnigent.runner.tool_execution_context import ToolExecutionContext
from omnigent.runtime import pending_elicitations
from omnigent.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    is_session_closed,
    title_without_closed_marker,
)
from omnigent.tools import ToolManager
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.async_inbox import (
    SysCallAsyncTool,
    SysCancelAsyncTool,
    SysCancelTaskTool,
    SysReadInboxTool,
)
from omnigent.tools.builtins.download_file import DownloadFileTool
from omnigent.tools.builtins.list_comments import ListCommentsTool
from omnigent.tools.builtins.os_env import (
    SysOsEditTool,
    SysOsReadTool,
    SysOsShellTool,
    SysOsWriteTool,
)
from omnigent.tools.builtins.spawn import (
    # Shared contract values with the in-process sys_session_* tools. Imported
    # (not duplicated) so the runner's REST-backed peek clamps to the same
    # bounds the LLM-facing tool schema advertises and tombstones with the
    # same marker the in-process close writes.
    _ACTIVITY_MAX_CHARS,
    _CLOSED_TITLE_INFIX,
    _HISTORY_DEFAULT_TAIL,
    _clamp_tail_items,
)
from omnigent.tools.builtins.sys_terminal import (
    SysTerminalCloseTool,
    SysTerminalLaunchTool,
    SysTerminalListTool,
    SysTerminalReadTool,
    SysTerminalSendTool,
)
from omnigent.tools.builtins.update_comment import UpdateCommentTool
from omnigent.tools.builtins.upload_file import UploadFileTool, safe_resolve

_logger = logging.getLogger(__name__)
def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

async def _execute_list_models_tool(*, agent_spec: AgentSpecLike | None) -> str:
    """
    Dispatch ``sys_list_models``: per-worker model availability.

    Runs the enumeration off the event loop — provider resolution reads
    config files and the listing fetches hit provider HTTP APIs (TTL-
    cached in :mod:`omnigent.model_catalog`).

    :param agent_spec: The calling session's agent spec; its
        ``sub_agents`` define the worker rows.
    :returns: JSON mapping of worker name (plus ``"self"``) to its
        ``{source, verified, models, note}`` row, or an error string.
    """
    if agent_spec is None:
        return "Error: sys_list_models requires an agent spec"
    from omnigent.model_catalog import catalog_for_spec

    catalog = await asyncio.to_thread(catalog_for_spec, agent_spec)
    return json.dumps(catalog)

async def _execute_web_fetch_tool(
    args: dict[str, Any],
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
    agent_spec: AgentSpecLike | None,
    task_id: str | None,
    publish_event: Callable[[str, dict[str, Any]], None] | None = None,
    session_inbox: asyncio.Queue[dict[str, Any]] | None = None,
) -> str:
    """
    Dispatch a ``web_fetch`` tool call.

    Translates the user-facing ``query`` / ``url`` arguments into
    a ``sys_session_send`` invocation against the built-in
    ``__web_researcher`` sub-agent, then delegates to
    :func:`_execute_subagent_tool`. The session name embeds
    ``task_id`` so concurrent ``web_fetch`` calls from the same
    parent don't collide on the
    ``(parent_conversation_id, title)`` unique index that
    ``_execute_subagent_tool`` ultimately exercises via
    ``POST /v1/sessions``.

    :param args: Parsed LLM arguments — ``query`` (required) and
        optional ``url``.
    :param server_client: httpx client pointed at the Omnigent server.
    :param conversation_id: Parent session id,
        e.g. ``"conv_abc123"``.
    :param agent_spec: Parent agent's spec — used by the inner
        ``_execute_subagent_tool`` to resolve the sub-agent.
    :param task_id: Calling task id; used to discriminate parallel
        ``web_fetch`` invocations from the same parent.
    :param session_inbox: Parent inbox queue for delayed sub-agent
        completion delivery.
    :returns: The researcher's findings, or an error string.
    """
    from omnigent.tools.builtins.web_fetch import (
        RESEARCHER_NAME,
        build_web_fetch_prompt,
    )

    query = args.get("query")
    if not query:
        return "Error: 'query' parameter is required."
    url = args.get("url")
    prompt = build_web_fetch_prompt(str(query), str(url) if url else None)

    # Embed task_id so each web_fetch from the same parent gets a
    # distinct child conversation (the server enforces a partial
    # unique index on (parent_conversation_id, title) where
    # title="<tool>:<session>").
    session_name = f"web_fetch_{task_id or 'anon'}"

    return await _execute_subagent_tool(
        {
            "agent": RESEARCHER_NAME,
            "args": prompt,
            "title": session_name,
        },
        server_client=server_client,
        conversation_id=conversation_id,
        agent_spec=agent_spec,
        publish_event=publish_event,
        session_inbox=session_inbox,
    )

async def _execute_agent_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    server_client: httpx.AsyncClient | None,
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    runner_workspace: Path | None,
) -> str:
    """
    Runner-local handler for ``sys_agent_get`` / ``sys_agent_download``.

    The runner has no in-process ``AgentStore`` / ``ArtifactStore``, so
    these proxy the Omnigent server's REST endpoints over ``server_client``:

    - ``sys_agent_get`` → ``GET /v1/sessions/{id}/agent`` (project the
      :class:`~omnigent.server.schemas.AgentObject`)
    - ``sys_agent_download`` → ``GET /v1/sessions/{id}/agent/contents``,
      write the ``.tar.gz`` into the agent's local os_env cwd, return the
      path
    - ``sys_agent_list`` → ``GET /v1/agents`` + ``GET /v1/sessions`` +
      local-config scan (no ``session_id``)

    :param tool_name: ``"sys_agent_get"``, ``"sys_agent_download"``, or
        ``"sys_agent_list"``.
    :param args: Parsed tool arguments; ``session_id`` required for
        get/download, ignored for list.
    :param server_client: HTTP client pointed at the Omnigent server; ``None``
        returns an error string.
    :param agent_spec: The running agent's spec — used (with
        ``conversation_id`` / ``runner_workspace``) to resolve the
        os_env cwd that ``sys_agent_download`` writes into and
        ``sys_agent_list`` scans for local configs.
    :param conversation_id: The caller's session id, for os_env cwd
        resolution, e.g. ``"conv_abc123"``.
    :param runner_workspace: The runner's workspace dir, authoritative
        for the os_env cwd when present.
    :returns: Tool output JSON string.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    if tool_name == "sys_agent_list":
        return await _agent_list_via_rest(
            server_client,
            agent_spec=agent_spec,
            conversation_id=conversation_id,
            runner_workspace=runner_workspace,
        )
    session_id = args.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return json.dumps({"error": f"{tool_name} requires a non-empty 'session_id' string"})
    if tool_name == "sys_agent_get":
        return await _agent_get_via_rest(session_id, server_client)
    return await _agent_download_via_rest(
        session_id,
        args,
        server_client,
        agent_spec=agent_spec,
        conversation_id=conversation_id,
        runner_workspace=runner_workspace,
    )

async def _agent_get_via_rest(
    session_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Return a session's bound-agent metadata via ``GET .../agent``.

    Projects the :class:`~omnigent.server.schemas.AgentObject` fields
    the orchestrator cares about: agent id, name, version, description,
    harness, MCP server summaries, and guardrail policy summaries. Maps a
    404 to ``agent_not_found`` and 401/403 to ``access_denied``.

    :param session_id: The session whose bound agent to inspect, e.g.
        ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON agent-metadata object, or a JSON error object.
    """
    try:
        resp = await server_client.get(f"/v1/sessions/{session_id}/agent", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_agent_get failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "agent_not_found", "session_id": session_id})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "session_id": session_id})
    if resp.status_code != 200:
        return json.dumps({"error": f"sys_agent_get returned {resp.status_code}"})
    agent: dict[str, Any] = resp.json()
    return json.dumps(
        {
            "session_id": session_id,
            "agent_id": agent.get("id"),
            "name": agent.get("name"),
            "version": agent.get("version"),
            "description": agent.get("description"),
            "harness": agent.get("harness"),
            "mcp_servers": agent.get("mcp_servers") or [],
            "policies": agent.get("policies") or [],
        }
    )

def _agent_bundle_filename(
    dest_filename: Any,
    agent_name: str,
    agent_version: str,
) -> str | None:
    """
    Resolve the local filename for a downloaded agent bundle.

    Uses the caller's ``dest_filename`` when given, else defaults to
    ``"<agent_name>-v<version>.tar.gz"``. The result must be a bare
    filename — any path separator (a traversal attempt) is rejected by
    returning ``None`` so the caller surfaces an error rather than
    writing outside the working directory.

    :param dest_filename: Caller-supplied filename, or ``None`` to use
        the default. Anything non-str is treated as absent.
    :param agent_name: Agent name from the ``X-Agent-Name`` header, e.g.
        ``"researcher"``.
    :param agent_version: Agent version from the ``X-Agent-Version``
        header, e.g. ``"3"``.
    :returns: A safe bare filename, or ``None`` when ``dest_filename``
        contains a path separator or is ``"."`` / ``".."``.
    """
    if isinstance(dest_filename, str) and dest_filename:
        if "/" in dest_filename or "\\" in dest_filename or dest_filename in (".", ".."):
            return None
        return dest_filename
    safe_name = (
        "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in agent_name) or "agent"
    )
    return f"{safe_name}-v{agent_version}.tar.gz"

async def _agent_download_via_rest(
    session_id: str,
    args: dict[str, Any],
    server_client: httpx.AsyncClient,
    *,
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    runner_workspace: Path | None,
) -> str:
    """
    Download a session's agent bundle and write it to the agent's disk.

    Fetches the ``.tar.gz`` from ``GET /v1/sessions/{id}/agent/contents``
    and writes the bytes into the agent's os_env working directory — the
    same cwd the agent's ``sys_os_*`` tools operate on (resolved via
    :func:`_effective_runner_os_env_spec`, so a ``caller_process``
    os_env's cwd is the ``runner_workspace`` or the per-conversation
    tmpdir). The default filename is ``"<agent_name>-v<version>.tar.gz"``
    (from the ``X-Agent-*`` response headers); a caller-supplied
    ``dest_filename`` overrides it. Returns the written path so the
    orchestrator can extract (``sys_os_shell``) and read
    (``sys_os_read``) the bundle.

    NOTE: writing through the resolved cwd is correct for the default
    ``caller_process`` os_env (a real local directory). A non-local
    sandbox whose filesystem differs from the runner's would not see the
    file; such os_env types are out of scope for v1 agent download.

    Maps a 404 to ``agent_not_found`` and 401/403 to ``access_denied``.

    :param session_id: The session whose agent bundle to download.
    :param args: Parsed tool arguments; optional ``dest_filename``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_spec: The running agent's spec, for os_env resolution.
    :param conversation_id: The caller's session id, for os_env cwd.
    :param runner_workspace: The runner workspace, authoritative cwd.
    :returns: JSON ``{path, agent_name, agent_version, bytes_written}``,
        or a JSON error object.
    """
    try:
        resp = await server_client.get(f"/v1/sessions/{session_id}/agent/contents", timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_agent_download failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "agent_not_found", "session_id": session_id})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "session_id": session_id})
    if resp.status_code != 200:
        return json.dumps({"error": f"sys_agent_download returned {resp.status_code}"})
    agent_name = resp.headers.get("X-Agent-Name", "agent")
    agent_version = resp.headers.get("X-Agent-Version", "0")
    filename = _agent_bundle_filename(args.get("dest_filename"), agent_name, agent_version)
    if filename is None:
        return json.dumps(
            {"error": "sys_agent_download dest_filename must be a bare filename, not a path"}
        )
    spec = _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
    cwd = Path(spec.cwd)
    await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
    # Resolve symlinks on the realized cwd and confirm the destination
    # stays within it before writing. ``filename`` is already a bare name
    # (``_agent_bundle_filename`` rejects separators), but a symlinked cwd
    # could still redirect the write outside the sandbox — realpath the
    # parent and check containment, matching the sys_os_write pattern.
    resolved_cwd = cwd.resolve()
    dest = (resolved_cwd / filename).resolve()
    if not dest.is_relative_to(resolved_cwd):
        return json.dumps(
            {"error": "sys_agent_download resolved destination escapes the working directory"}
        )
    await asyncio.to_thread(dest.write_bytes, resp.content)
    return json.dumps(
        {
            "path": str(dest),
            "agent_name": agent_name,
            "agent_version": agent_version,
            "bytes_written": len(resp.content),
        }
    )

async def _agent_list_fetch(
    path: str,
    server_client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """
    Fetch one page of a paginated list endpoint, returning its ``data``.

    Best-effort: returns ``[]`` on transport error or non-200 so a single
    failing source degrades ``sys_agent_list`` to "that section is empty"
    rather than failing the whole call.

    :param path: The list endpoint path, e.g. ``"/v1/agents"`` or
        ``"/v1/sessions"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The ``data`` list from the paginated response (possibly
        empty).
    """
    try:
        resp = await server_client.get(
            path,
            params={"limit": _AGENT_LIST_PAGE_LIMIT, "order": "desc"},
            timeout=30.0,
        )
    except Exception:  # noqa: BLE001
        return []
    if resp.status_code != 200:
        return []
    data = resp.json().get("data", [])
    return data if isinstance(data, list) else []

def _scan_local_agent_configs(configs_dir: Path) -> list[dict[str, str | None]]:
    """
    Scan a directory for locally-authored agent config YAMLs.

    Reads each ``*.yaml`` under ``configs_dir`` (the agent-config subdir
    of the os_env cwd), extracting ``name`` and ``description`` for the
    listing. Files that don't parse to a mapping are skipped (defensive —
    a stray non-config YAML shouldn't break the scan). Returns ``[]``
    when the directory doesn't exist yet (no configs authored).

    :param configs_dir: The agent-config directory to scan, e.g.
        ``<cwd>/.omnigent/agent-configs``.
    :returns: ``[{"name", "path", "description"}, ...]``, sorted by path.
    """
    import yaml

    if not configs_dir.is_dir():
        return []
    entries: list[dict[str, str | None]] = []
    for path in sorted(configs_dir.glob("*.yaml")):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(loaded, dict):
            continue
        entries.append(
            {
                "name": loaded.get("name"),
                "path": str(path),
                "description": loaded.get("description"),
            }
        )
    return entries

async def _agent_list_via_rest(
    server_client: httpx.AsyncClient,
    *,
    agent_spec: AgentSpecLike | None,
    conversation_id: str | None,
    runner_workspace: Path | None,
) -> str:
    """
    List launchable agents across built-ins, session-bound, and local.

    Fans out three independent reads — each degrades to an empty section
    on failure rather than failing the whole call:

    - ``builtins``: ``GET /v1/agents`` (template agents), projected to
      ``{agent_id, name, description, harness}``.
    - ``session_agents``: ``GET /v1/sessions``, projected to
      ``{session_id, agent_id, agent_name, status}`` so the caller can
      launch the agent directly (``sys_session_create`` by
      ``agent_id``) or ``sys_agent_get`` / ``sys_agent_download`` a
      chosen session.
    - ``local_configs``: a scan of the os_env cwd's agent-config subdir
      (YAMLs authored with ``sys_os_write`` per the agent-authoring
      skill), projected to ``{name, path, description}``.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_spec: The running agent's spec, for os_env cwd
        resolution of the local-config scan.
    :param conversation_id: The caller's session id, for os_env cwd.
    :param runner_workspace: The runner workspace, authoritative cwd.
    :returns: JSON ``{builtins, session_agents, local_configs}``.
    """
    builtins_raw = await _agent_list_fetch("/v1/agents", server_client)
    sessions_raw = await _agent_list_fetch("/v1/sessions", server_client)
    spec = _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
    configs_dir = Path(spec.cwd) / _AGENT_CONFIG_SUBDIR
    local_configs = await asyncio.to_thread(_scan_local_agent_configs, configs_dir)
    return json.dumps(_project_agent_list(builtins_raw, sessions_raw, local_configs))

def _project_agent_list(
    builtins_raw: list[dict[str, Any]],
    sessions_raw: list[dict[str, Any]],
    local_configs: list[dict[str, str | None]],
) -> dict[str, list[dict[str, Any]]]:
    """
    Project the three raw ``sys_agent_list`` sources into the tool result.

    Built-in :class:`AgentObject` rows are projected to
    ``{agent_id, name, description, harness}`` (note ``id`` → ``agent_id``
    for naming consistency with the rest of the surface); session rows to
    ``{session_id, agent_id, agent_name, status}``; local configs pass
    through unchanged.

    :param builtins_raw: ``data`` rows from ``GET /v1/agents``.
    :param sessions_raw: ``data`` rows from ``GET /v1/sessions``.
    :param local_configs: Entries from :func:`_scan_local_agent_configs`.
    :returns: ``{builtins, session_agents, local_configs}``.
    """
    builtins = [
        {
            "agent_id": a.get("id"),
            "name": a.get("name"),
            "description": a.get("description"),
            "harness": a.get("harness"),
        }
        for a in builtins_raw
    ]
    session_agents = [
        {
            "session_id": s.get("id"),
            "agent_id": s.get("agent_id"),
            "agent_name": s.get("agent_name"),
            "status": s.get("status"),
        }
        for s in sessions_raw
    ]
    return {
        "builtins": builtins,
        "session_agents": session_agents,
        "local_configs": local_configs,
    }

def _skill_scope_matches(agent: dict[str, Any], scope: str) -> bool:
    """
    Whether *agent* falls within a scope phrase (mirrors skills_mcp).

    Scope grammar: ``organization`` (all non-workflow agents),
    ``department:<name>``, ``employee:<id-or-name>``, or a bare agent id /
    display name. Workflow/orchestrator agents are excluded from
    ``organization`` / ``department`` by default.

    :param agent: One row from ``GET /v1/agents``.
    :param scope: The scope phrase to match against.
    :returns: ``True`` when the agent is in scope.
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
    # Match the generated id, the stable `name` slug, OR the display name — a
    # built-in's id is a generated ag_… hash, so the slug (e.g.
    # "structured-output-demo") is the handle the user / the LLM actually names.
    return target in (
        str(agent.get("id") or "").lower(),
        str(agent.get("name") or "").lower(),
        str(agent.get("display_name") or "").lower(),
    )

def _skill_body(resp: httpx.Response) -> dict[str, Any]:
    """
    Decode a skills-route JSON body, raising on a non-2xx with the detail.

    :param resp: The httpx response from a ``/v1/skills/*`` or ``/v1/agents``
        call.
    :returns: The decoded JSON object.
    :raises RuntimeError: on a non-2xx status, carrying the server detail.
    """
    if not (200 <= resp.status_code < 300):
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        detail = payload.get("detail") if isinstance(payload, dict) else resp.text[:200]
        raise RuntimeError(f"server returned {resp.status_code}: {detail}")
    return resp.json()

async def _execute_skill_acq_tool(
    tool_name: str,
    args: dict[str, Any],
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for the ``sys_skill_*`` family (BDP-2487).

    Each tool proxies one of the Omnigent server's existing ``/v1/skills/*``
    routes (or ``/v1/agents`` for scope resolution) over ``server_client``,
    which carries the runner tunnel token so the ``require_user`` mutating
    routes (previews / apply) resolve the session owner and pass — the same
    posture as :func:`_execute_agent_tool`. Reuses the request shapes and
    response-unwrapping of ``bytedesk_omnigent.skills_mcp``.

    :param tool_name: One of the seven ``sys_skill_*`` names.
    :param args: Parsed tool arguments from the LLM.
    :param server_client: HTTP client pointed at the Omnigent server; ``None``
        returns a JSON error object.
    :returns: Tool output JSON string.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    try:
        if tool_name == "sys_skill_search":
            body: dict[str, Any] = {"query": args.get("query"), "limit": args.get("limit", 20)}
            if args.get("sources") is not None:
                body["sources"] = args["sources"]
            resp = await server_client.post("/v1/skills/search", json=body, timeout=60.0)
            out = _skill_body(resp)
            return json.dumps({"results": out.get("data", []), "errors": out.get("errors", [])})

        if tool_name == "sys_skill_sources":
            out = _skill_body(await server_client.get("/v1/skills/sources", timeout=60.0))
            return json.dumps({"sources": out.get("data", [])})

        if tool_name == "sys_skill_installed":
            agent_id = args.get("agent_id")
            params = {"agent_id": agent_id} if agent_id else None
            out = _skill_body(
                await server_client.get("/v1/skills/installed", params=params, timeout=60.0)
            )
            return json.dumps({"installed": out.get("data", [])})

        if tool_name == "sys_skill_resolve_targets":
            scope = args.get("scope")
            if not isinstance(scope, str) or not scope:
                return json.dumps({"error": "sys_skill_resolve_targets requires a 'scope' string"})
            out = _skill_body(
                await server_client.get(
                    "/v1/agents", params={"limit": 1000, "order": "asc"}, timeout=60.0
                )
            )
            targets = [
                {
                    "id": a.get("id"),
                    "agent_ref": a.get("name") or a.get("id"),
                    "name": a.get("name"),
                    "display_name": a.get("display_name"),
                    "department": a.get("department"),
                }
                for a in out.get("data", [])
                if _skill_scope_matches(a, scope)
            ]
            return json.dumps({"targets": targets})

        if tool_name == "sys_skill_stage_preview":
            preview_body = {
                "operation": "install",
                "target_agent_ids": args.get("target_agent_ids", []),
                "install_mode": args.get("install_mode", "skip_existing"),
                "source": args.get("source"),
                "source_ref": args.get("source_ref"),
            }
            selected_skill_names = args.get("selected_skill_names")
            if selected_skill_names:
                preview_body["selected_skill_names"] = selected_skill_names
            out = _skill_body(
                await server_client.post("/v1/skills/previews", json=preview_body, timeout=60.0)
            )
            return json.dumps(
                {
                    "preview_id": out.get("id"),
                    "skills": out.get("skills", []),
                    "target_actions": out.get("target_actions", []),
                }
            )

        if tool_name == "sys_skill_apply":
            preview_id = args.get("preview_id")
            if not isinstance(preview_id, str) or not preview_id:
                return json.dumps({"error": "sys_skill_apply requires a 'preview_id' string"})
            apply_body: dict[str, Any] = {}
            if args.get("agent_ids") is not None:
                apply_body["target_agent_ids"] = args["agent_ids"]
            out = _skill_body(
                await server_client.post(
                    f"/v1/skills/previews/{preview_id}/apply", json=apply_body, timeout=60.0
                )
            )
            return json.dumps({"results": out.get("data", [])})

        # sys_skill_remove: stage a remove preview, then apply it.
        skill_name = args.get("skill_name")
        target_agent_ids = args.get("target_agent_ids", [])
        if not isinstance(skill_name, str) or not skill_name:
            return json.dumps({"error": "sys_skill_remove requires a 'skill_name' string"})
        preview = _skill_body(
            await server_client.post(
                "/v1/skills/previews",
                json={
                    "operation": "remove",
                    "target_agent_ids": target_agent_ids,
                    "skill_names": [skill_name],
                },
                timeout=60.0,
            )
        )
        out = _skill_body(
            await server_client.post(
                f"/v1/skills/previews/{preview.get('id')}/apply",
                json={"target_agent_ids": target_agent_ids},
                timeout=60.0,
            )
        )
        return json.dumps({"results": out.get("data", [])})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{tool_name} failed: {exc}"})

def _execute_skill_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    agent_spec: AgentSpecLike | None,
    runner_workspace: Path | None,
    acting_identity: ActingIdentity | None = None,
) -> str:
    """
    Runner-local handler for ``load_skill`` and ``read_skill_file``.

    Instantiates the tool with the agent spec's bundled skills
    plus host-scope discovery from the runner workspace, then
    invokes it.

    :param tool_name: ``"load_skill"`` or ``"read_skill_file"``.
    :param args: Parsed JSON arguments from the LLM.
    :param agent_spec: The session's AgentSpec.
    :param runner_workspace: The runner's workspace path for
        host-scope skill discovery.
    :returns: Tool output string.
    """
    from omnigent.tools.builtins.load_skill import LoadSkillTool
    from omnigent.tools.builtins.read_skill_file import ReadSkillFileTool

    bundled_skills = list(getattr(agent_spec, "skills", None) or [])
    skills_filter = getattr(agent_spec, "skills_filter", "all")
    # Auto-inject the build-omnigent skill for agents that opt into the
    # orchestration surface (tools.agents). This teaches the LLM how to
    # author valid agent configs via sys_os_write without requiring the
    # agent's own bundle to ship a skills/ directory.
    bundled_skills = _inject_orchestrator_skills(bundled_skills, agent_spec)

    if tool_name == "load_skill":
        tool = LoadSkillTool(
            bundled_skills,
            agent_root=runner_workspace,
            skills_filter=skills_filter,
        )
    else:
        tool = ReadSkillFileTool(bundled_skills)

    arguments_json = json.dumps(args)
    from omnigent.tools.base import ToolContext

    ctx = ToolContext(task_id="", conversation_id="", agent_id="", acting_identity=acting_identity)
    return tool.invoke(arguments_json, ctx)

