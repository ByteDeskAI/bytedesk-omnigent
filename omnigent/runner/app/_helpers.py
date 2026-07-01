"""Runner FastAPI app — spawns harness subprocesses and dispatches to them.

Per ``designs/RUNNER.md`` §1, the runner owns harness subprocesses.
It resolves the harness type + spawn-env from the agent spec (either
via a spec_resolver callback for in-process use, or via
GET /v1/agents/{id}/contents for out-of-process use).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import mimetypes
import os
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only import: the runner keeps codex deps out of its runtime import
    # graph (they are imported lazily inside the codex-native helpers).
    from omnigent.codex_native_app_server import CodexAppServerClient
    from omnigent.runner.cost_advisor import AdvisorTurnResult

    # Boundary payload TypedDicts (sweep-2 BDP-2366). Imported type-only so
    # the runtime ``app`` <-> ``tool_dispatch`` import stays lazy (the cycle
    # both modules already break with function-level imports).
    from omnigent.runner.tool_dispatch import (
        SessionSnapshotPayload,
        SubagentInboxPayload,
    )
    from omnigent.terminals.registry import TerminalListEntry

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    SessionResourceView,
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.model_override import validate_model_override
from omnigent.runner import pending_approvals
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.runner.subagent_status import (
    _TERMINAL as _SUBAGENT_TERMINAL_STATUSES,
)
from omnigent.runner.subagent_status import (
    SubagentWorkStatus,
    TerminalStatus,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_NOT_FOUND,
    bridge_tmux_pty_to_websocket,
)
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
)

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

def _client_safe_error_detail(exc: BaseException, *, context: str) -> str:
    """
    Log *exc* in full and return a generic detail string safe for clients.

    Raw exception text (``str(exc)``) can embed absolute paths, internal
    hostnames, PIDs, and other server-side state. The runner is reached via
    the AP server proxy and its error bodies are relayed to the caller, so
    the cause is logged here for operators while the HTTP response carries
    only this fixed string. The structured ``error`` code that accompanies
    the detail already names the failure category for the caller.

    :param exc: The caught exception, e.g. a ``RuntimeError`` from a harness
        spawn or an ``InvalidPath`` from path validation.
    :param context: Short operator-facing label for the failing operation,
        e.g. ``"harness spawn"``. Appears only in the server log.
    :returns: A fixed, non-sensitive string safe to return to clients.
    """
    _logger.warning("%s failed: %s", context, exc, exc_info=True)
    return "Request failed on the runner; see runner logs for details."

SpecResolver = Callable[[str, str | None], Awaitable[Any | None]]

_NO_BODY_STATUS_CODES = {204, 304}

_SESSION_STREAM_HEARTBEAT_S = 15.0

def _get_runner_llm_client() -> Any:
    """Return the runner-process LLM client, creating it on first use.

    The client is constructed from the runner process's environment
    variables, which include the Databricks credentials set up by the
    runner entry point. This is intentionally separate from the AP
    server's ``_get_llm_client()`` — the runner may have different
    (or more) credentials than the Omnigent server.

    :returns: A ``llms.Client`` instance bound to this runner process.
    """
    global _runner_llm_client
    if _runner_llm_client is None:
        from omnigent.llms import Client as LLMClient

        _runner_llm_client = LLMClient()
    return _runner_llm_client

def _required_runner_env(name: str) -> str:
    """
    Return a required runner environment variable.

    :param name: Environment variable name, e.g. ``"RUNNER_SERVER_URL"``.
    :returns: Non-empty environment variable value.
    :raises RuntimeError: If the variable is missing or empty.
    """
    value = os.environ.get(name)
    if value is None or not value:
        raise RuntimeError(f"{name} must be set for runner-owned Codex terminals.")
    return value

def _pi_session_workspace(session_workspace: str | None) -> Path:
    """
    Resolve the cwd for a runner-owned Pi terminal.

    :param session_workspace: Session ``workspace`` from the server snapshot.
    :returns: Workspace path for the terminal cwd.
    """
    raw = session_workspace or _required_runner_env("OMNIGENT_RUNNER_WORKSPACE")
    return Path(raw.strip()).expanduser().resolve()

def _pi_args_have_session_control(args: list[str]) -> bool:
    """
    Return whether user Pi args already specify session behavior.

    :param args: User pass-through Pi CLI args.
    :returns: ``True`` when Omnigent should not add resume/session flags.
    """
    session_flags = {
        "--session-dir",
        "--session",
        "--continue",
        "--resume",
        "--fork",
        "--no-session",
    }
    for arg in args:
        if arg in session_flags:
            return True
        if arg.startswith(("--session-dir=", "--session=")):
            return True
    return False

def _pi_args_have_provider(args: list[str]) -> bool:
    """Return whether user Pi args already pin a provider/model/key.

    When the user passes their own ``--provider`` / ``--model`` / ``--api-key``,
    Omnigent must not inject the ``omnigent setup`` provider on top — the
    explicit choice wins.

    :param args: User pass-through Pi CLI args.
    :returns: ``True`` when Omnigent should not add provider/model args.
    """
    provider_flags = {"--provider", "--model", "--api-key"}
    for arg in args:
        if arg in provider_flags:
            return True
        if arg.startswith(("--provider=", "--model=", "--api-key=")):
            return True
    return False

async def _session_payload_for_host_spawn_check(
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> dict[str, Any] | None:
    """
    Fetch a session snapshot for Codex host-spawn detection.

    :param server_client: The runner's Omnigent server HTTP client, or
        ``None`` in embedded/test setups.
    :param session_id: Session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: Parsed session JSON object, or ``None`` when the
        snapshot cannot be retrieved.
    """
    if server_client is None:
        return None
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.warning(
            "Could not resolve host_id for %s; skipping codex terminal auto-create",
            session_id,
        )
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload

async def _fetch_cost_control_mode_override(
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> str | None:
    """
    Read the session's per-session Cost Optimized toggle, defensively.

    Fetches the session snapshot and returns its
    ``cost_control_mode_override``. Treats every failure mode
    — no client, transport error, non-200, absent field — as ``None``
    (no override) so the advisor still works against an older server
    that lacks the column. The advisor never blocks on this read.

    :param server_client: The runner's Omnigent server HTTP client, or
        ``None`` in embedded / test setups.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: ``"on"`` / ``"off"`` when the session set the toggle, or
        ``None`` (unset, or unreadable for any reason).
    """
    payload = await _session_payload_for_host_spawn_check(server_client, session_id)
    if payload is None:
        return None
    override = payload.get("cost_control_mode_override")
    return override if isinstance(override, str) else None

def _ensure_orchestrator_skills_in_bundle(
    bundle_dir: Path,
    agent_spec: Any,
) -> None:
    """
    Link the ``build-omnigent`` skill into a bundle's ``skills/`` dir.

    Called before native bridge launches so ``--plugin-dir`` (claude) or
    ``CODEX_HOME/skills/`` (codex) picks up the skill. Injects
    unconditionally for every agent — every ``omnigent claude`` /
    ``omnigent codex`` user should be able to author new agents. The
    skill isn't already present guard is idempotent. Best-effort: a
    failure to link is logged but does not abort the terminal launch.

    :param bundle_dir: Materialized agent-bundle root, e.g.
        ``/tmp/omnigent-ap-chat-xyz/bundle``.
    :param agent_spec: The session's AgentSpec (unused after gate
        removal; retained for call-site compat).
    """
    del agent_spec  # no longer gated; inject unconditionally
    skill_name = "build-omnigent"
    target_dir = bundle_dir / "skills" / skill_name
    if target_dir.exists():
        return
    source = (
        Path(__file__).resolve().parent.parent / "onboarding" / "agent" / "skills" / skill_name
    )
    if not source.is_dir():
        return
    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        target_dir.symlink_to(source)
    except OSError:
        _logger.debug(
            "Could not link %s skill into bundle %s",
            skill_name,
            bundle_dir,
            exc_info=True,
        )

_SESSION_LABEL_LOOKUP_TIMEOUT_SECONDS = 1.0

async def _session_labels_for_runner_spawn(
    *,
    server_client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, str]:
    """
    Fetch session labels for harness spawn-env construction.

    :param server_client: Omnigent server client used to fetch the session
        labels endpoint.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: String label mapping. Empty on lookup failure.
    """
    path = f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/labels"
    try:
        resp = await server_client.get(
            path,
            timeout=_SESSION_LABEL_LOOKUP_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        _logger.debug(
            "Timed out resolving session labels; session=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        return {}
    except httpx.HTTPError as exc:
        _logger.warning(
            "Failed to resolve session labels; session=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        return {}
    if resp.status_code != 200:
        _logger.warning(
            "Failed to resolve session labels; session=%s status=%s",
            session_id,
            resp.status_code,
        )
        return {}
    try:
        labels = resp.json().get("labels")
    except ValueError:
        # A 200 with a non-JSON body (e.g. an empty response from the
        # Databricks Apps proxy when the server event loop is starved,
        # or an HTML login page on an auth edge) must not abort the
        # turn. Labels are a best-effort spawn hint; recover by using
        # the session id, exactly as the timeout / non-200 paths do.
        _logger.warning(
            "Session labels response was not valid JSON; session=%s status=%s",
            session_id,
            resp.status_code,
        )
        return {}
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}

_RUNNER_DISPATCHED_FIELD = "omnigent_runner_dispatched"

def _response_body_preview(resp: Any, *, limit: int = 500) -> str:
    """
    Return a short response-body preview for diagnostics.

    Some runner tests use lightweight response fakes that expose
    ``content`` and ``status_code`` but not HTTPX's convenience
    ``text`` property. Logging should not make those fakes diverge from
    production behavior.

    :param resp: Response-like object, e.g. ``httpx.Response``.
    :param limit: Maximum number of characters to include.
    :returns: Decoded response text preview.
    """
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text[:limit]
    content = getattr(resp, "content", b"")
    if isinstance(content, bytes):
        return content[:limit].decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content[:limit]
    return ""

_CHILD_PREVIEW_MAX_CHARS = 150

def _session_status_to_task_status(status: object) -> str | None:
    """
    Map a ``session.status`` value to a child summary ``current_task_status``.

    The two vocabularies differ (session status vs. task status); this
    keeps the child rail's status text roughly in sync as ``busy`` flips.

    :param status: A ``session.status`` value, e.g. ``"running"``.
    :returns: ``"launching"`` / ``"in_progress"`` / ``"completed"`` /
        ``"failed"``, or ``None`` for an unrecognized status (caller
        omits the field).
    """
    if status == "launching":
        return "launching"
    if status in ("running", "waiting"):
        return "in_progress"
    if status == "idle":
        return "completed"
    if status == "failed":
        return "failed"
    return None

def _normalize_turn_error(error: dict[str, Any]) -> dict[str, str]:
    """
    Coerce a turn-failure ``error`` dict into a ``{code, message}`` shape.

    The ``error`` dicts passed to :func:`_on_proxy_stream_end` vary by
    call site: most carry ``{"message": "..."}`` (and sometimes
    ``"type"``), but a few carry only ``{"status": <http status>}``.
    The wire ``SessionStatusEvent.error`` field (``ErrorDetail``)
    requires both ``code`` and ``message``, so this normalizes every
    shape into one the schema accepts, never raising on a missing key.
    The result is what gets published on the ``failed`` status event
    and ultimately rendered as the REPL's terminal error line.

    :param error: Raw error dict from a ``_on_proxy_stream_end`` call,
        e.g. ``{"message": "turn setup failed: ..."}`` or
        ``{"status": 502}``.
    :returns: A dict with ``code`` and ``message`` string keys, e.g.
        ``{"code": "runner_error", "message": "turn setup failed: ..."}``.
        Falls back to a generic message when none is present.
    """
    raw_message = error.get("message")
    if isinstance(raw_message, str) and raw_message.strip():
        message = raw_message
    elif "status" in error:
        message = f"turn failed (status {error['status']})"
    else:
        message = "turn failed"
    raw_code = error.get("type")
    code = raw_code if isinstance(raw_code, str) and raw_code else "runner_error"
    return {"code": code, "message": message}

def _truncate_child_preview(text: str) -> str:
    """
    Truncate a child message preview to the cap with an ellipsis.

    Matches the server-side ``_latest_message_preview`` truncation so the
    live runner-pushed preview and the snapshot preview look the same.

    :param text: The child's latest assistant reply text.
    :returns: ``text`` truncated to :data:`_CHILD_PREVIEW_MAX_CHARS` with
        a trailing ellipsis when longer, else ``text`` unchanged.
    """
    if len(text) > _CHILD_PREVIEW_MAX_CHARS:
        return text[:_CHILD_PREVIEW_MAX_CHARS].rstrip() + "…"
    return text

def get_session_agent_id(session_id: str) -> str | None:
    """
    Return the durable agent_id for a session.

    :param session_id: Session/conversation ID, e.g.
        ``"conv_abc123"``.
    :returns: The agent_id, or ``None`` if not found.
    """
    return _session_agent_ids_ref.get(session_id)

def create_runner_app_from_env() -> FastAPI:
    """Lightweight uvicorn ``--factory`` entry point for transport subprocesses.

    Reads ``RUNNER_SERVER_URL`` from the environment and constructs a
    minimal :class:`httpx.AsyncClient` for the Omnigent server, then delegates
    to :func:`create_runner_app` with no :class:`HarnessProcessManager`,
    no spec resolver, and no terminal registry.

    Used as the default ``app_factory_path`` for
    :class:`~omnigent.runner.transports.tcp.RunnerTCPSubprocess` and
    :class:`~omnigent.runner.transports.uds.RunnerSubprocess`.  It is
    intentionally lighter than :func:`omnigent.runner._entry.create_app`
    so transport smoke tests start quickly without spawning harness pools
    or sweeping orphan directories.

    :returns: A :class:`FastAPI` runner app backed by an httpx client
        pointed at ``RUNNER_SERVER_URL``.
    :raises RuntimeError: If ``RUNNER_SERVER_URL`` is not set in the
        environment.
    """
    import os

    import httpx

    server_url = os.environ.get("RUNNER_SERVER_URL", "").strip()
    if not server_url:
        raise RuntimeError("RUNNER_SERVER_URL is required for the runner subprocess factory")
    server_client = httpx.AsyncClient(
        base_url=server_url,
        timeout=httpx.Timeout(5.0, read=None),
    )
    return create_runner_app(server_client=server_client)

def _apply_sandbox_override_from_verdict(
    spec: Any,
    verdict_data: Any,
) -> None:
    """Apply sandbox override from a policy verdict's ``data`` field.

    The ``enforce_sandbox`` policy returns replacement ``data`` shaped
    as ``{"name": "sys_agent_start", "arguments": {"sandbox": {...}}}``.
    This extracts the ``sandbox`` dict and mutates ``spec.os_env``
    in-place.

    :param spec: The agent spec (``AgentSpec``) — mutated in-place.
    :param verdict_data: The ``PolicyVerdict.data`` payload, expected
        to be a dict with ``arguments.sandbox``.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    if not isinstance(verdict_data, dict):
        return
    args = verdict_data.get("arguments")
    if not isinstance(args, dict):
        return
    sandbox_override = args.get("sandbox")
    if not isinstance(sandbox_override, dict):
        return

    if spec.os_env is None:
        spec.os_env = OSEnvSpec()
    if spec.os_env.sandbox is None:
        spec.os_env.sandbox = OSEnvSandboxSpec()

    for key, value in sandbox_override.items():
        if hasattr(spec.os_env.sandbox, key):
            setattr(spec.os_env.sandbox, key, value)

