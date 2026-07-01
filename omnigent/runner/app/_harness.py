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

def _forward_harness_response(resp: httpx.Response) -> Response:
    """Safely relay a non-streaming harness response through FastAPI.

    Starlette's ``JSONResponse(status_code=204, content=None)`` serializes
    ``None`` as ``b\"null\"``. Uvicorn correctly treats 204/304 as no-body
    responses and raises ``RuntimeError(\"Response content longer than
    Content-Length\")`` when any bytes are sent. Return a plain empty
    ``Response`` for no-body status codes (204/304).  For other statuses with
    an empty body, forward an explicit empty body while preserving
    ``content-type`` so callers can distinguish e.g. a 200 with no payload
    from a 204.
    """
    if resp.status_code in _NO_BODY_STATUS_CODES:
        return Response(status_code=resp.status_code)

    content_type = resp.headers.get("content-type", "")

    if not resp.content:
        return Response(
            content=b"",
            status_code=resp.status_code,
            media_type=content_type or None,
        )

    if "application/json" in content_type.lower():
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except ValueError:
            # Fall through to raw bytes if an upstream mislabels non-JSON content.
            pass

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type or None,
    )

class ResolvedSpec:
    spec: Any
    workdir: Path

    def __getattr__(self, name: str) -> Any:
        return getattr(self.spec, name)

def _unwrap_resolved_spec(entry: Any) -> Any:
    return entry.spec if isinstance(entry, ResolvedSpec) else entry

def _resolved_spec_workdir(entry: Any) -> Path | None:
    return entry.workdir if isinstance(entry, ResolvedSpec) else None

async def _resolve_harness_config(
    *,
    agent_id: str | None,
    spec_resolver: SpecResolver | None,
    session_id: str | None = None,
    model_override: str | None = None,
    harness_override: str | None = None,
    sub_agent_name: str | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Resolve harness type + spawn-env from the agent spec.

    :param agent_id: Agent id to resolve the spec for.
    :param spec_resolver: Resolver that returns the spec for *agent_id*.
    :param session_id: Session/conversation id, threaded to the resolver.
    :param model_override: Per-session ``/model`` override, applied to the
        spawn-env model so it takes effect on the SDK harnesses.
    :param harness_override: Per-session brain-harness override (validated
        at session create, forwarded by the server in the message body),
        e.g. ``"pi"``. Replaces the spec's ``executor.config.harness``.
    :param sub_agent_name: For a sub-agent session, the dispatched
        sub-agent's name (e.g. ``"claude_code"``). The bound *agent_id*
        resolves to the PARENT spec, so without this swap a child's turn
        resolves the parent's harness (``claude-sdk``) and the process
        manager respawns — tearing down the child's live ``claude-native``
        terminal ("Bridge closed: terminal resource not found"). When set,
        the parent spec is swapped to the matching sub-spec via
        :func:`_find_spec_by_name` before harness derivation. ``None`` for
        top-level sessions.
    :returns: ``(harness, spawn_env)``; a default for unresolved specs.
    """
    if agent_id and spec_resolver:
        spec_entry = await spec_resolver(agent_id, session_id)
        spec = _unwrap_resolved_spec(spec_entry)
        workdir = _resolved_spec_workdir(spec_entry)
        if spec is not None:
            # Swap to the sub-agent's own spec so its harness (not the
            # parent's) drives the turn. Mirrors the POST /v1/sessions and
            # _run_turn_bg swaps; applied here so the harness-HTTP path is
            # sub-agent-aware too, even after a reconnect drops the
            # in-memory _session_sub_agent_names map.
            if sub_agent_name:
                from omnigent.runtime.workflow import _find_spec_by_name

                sub_spec = _find_spec_by_name(spec, sub_agent_name)
                if sub_spec is not None:
                    spec = sub_spec
            harness = harness_override or spec.executor.config.get("harness") or spec.executor.type
            harness = canonicalize_harness(harness) or harness
            spawn_env = _build_spawn_env_from_spec(
                spec, harness, workdir=workdir, model_override=model_override
            )
            return harness, spawn_env

    # Fallback for tests that register a custom harness in _HARNESS_MODULES.
    return "runner-test-default", None

def _build_spawn_env_from_spec(
    spec: Any,
    harness: str,
    *,
    workdir: Path | None = None,
    model_override: str | None = None,
) -> dict[str, str] | None:
    """Build spawn-env from spec — mirrors workflow.py's helpers.

    :param spec: The resolved agent spec.
    :param harness: Canonical harness name, e.g. ``"claude-sdk"``.
    :param workdir: Bundle workdir, threaded to the builders.
    :param model_override: The per-session ``/model`` override, e.g.
        ``"claude-sonnet-4-6"``, or ``None``. When set, it overrides the
        ``HARNESS_<H>_MODEL`` the builder baked in (spec model / provider
        default / catalog default) so ``/model`` actually takes effect on
        the SDK / in-process harnesses. (The native CLIs honor the override
        via ``--model`` in :func:`_build_claude_native_base_args`; the
        SDK harnesses have no such arg, so the override must land in the
        env var here.)
    :returns: The spawn-env dict, or ``None`` for native / unknown harnesses.
    """
    try:
        from omnigent.runtime.workflow import (
            _build_antigravity_spawn_env,
            _build_claude_sdk_spawn_env,
            _build_codex_spawn_env,
            _build_cursor_spawn_env,
            _build_openai_agents_sdk_spawn_env,
            _build_pi_spawn_env,
        )

        if harness == "claude-sdk":
            env = _build_claude_sdk_spawn_env(spec, workdir=workdir)
        elif harness == "codex":
            env = _build_codex_spawn_env(spec, workdir=workdir)
        elif harness == "pi":
            env = _build_pi_spawn_env(spec, workdir=workdir)
        elif harness == "openai-agents":
            env = _build_openai_agents_sdk_spawn_env(spec)
        elif harness == "cursor":
            env = _build_cursor_spawn_env(spec, workdir=workdir)
        elif harness == "antigravity":
            env = _build_antigravity_spawn_env(spec)
        else:
            # Native terminal harnesses and unknown harnesses build env elsewhere.
            return None
    except ImportError:
        return None

    # Per-session ``/model`` override wins over everything the builder baked
    # into HARNESS_<H>_MODEL. Without this, `/model` is recorded in the
    # readout but the turn still uses the provider/catalog default.
    if model_override:
        model_key = _HARNESS_MODEL_ENV_KEY.get(harness)
        if model_key is not None:
            env[model_key] = model_override

    # Routing visibility: log the resolved gateway target so operators can
    # confirm which provider a turn actually hits (api.anthropic.com /
    # api.openai.com for a key, vs a Databricks profile). Logged here in the
    # runner process (INFO is emitted) rather than the harness subprocess
    # (which suppresses inner.* INFO). ``base_url`` is empty for the legacy
    # ``profile:`` path (resolved downstream by ucode); the profile still
    # identifies the Databricks target.
    if env is not None:
        prefix = f"HARNESS_{harness.upper().replace('-', '_')}"
        _logger.info(
            "%s gateway routing: gateway=%s base_url=%s profile=%s model=%s",
            harness,
            env.get(f"{prefix}_GATEWAY"),
            env.get(f"{prefix}_GATEWAY_BASE_URL"),
            env.get(f"{prefix}_DATABRICKS_PROFILE"),
            env.get(_HARNESS_MODEL_ENV_KEY.get(harness, f"{prefix}_MODEL")),
        )
    return env

async def _evaluate_agent_start_gate(
    spec: Any,
    harness: str,
) -> Any:
    """Evaluate ``__agent_start`` through the spec's policy gate.

    Constructs a :class:`RunnerToolPolicyGate` from the spec and
    evaluates a synthetic ``__agent_start`` tool call.  This reuses
    the same gate that guards MCP tool calls — no round-trip to the
    Omnigent server required.

    :param spec: The resolved agent spec (``AgentSpec``).
    :param harness: Canonical harness name, e.g. ``"claude-sdk"``.
    :returns: A :class:`PolicyVerdict` if the spec has guardrails
        policies, ``None`` if no policies apply.
    """
    from omnigent.runner.policy import RunnerToolPolicyGate

    gate = RunnerToolPolicyGate.from_spec(spec)
    if gate.is_empty:
        return None

    sandbox_dict: dict[str, Any] | None = None
    if spec.os_env is not None and spec.os_env.sandbox is not None:
        sandbox_dict = dataclasses.asdict(spec.os_env.sandbox)

    return await gate.evaluate_tool_call(
        "sys_agent_start",
        {
            "agent_name": getattr(spec, "name", None) or "",
            "harness": harness,
            "sandbox": sandbox_dict,
        },
    )

