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

async def _evaluate_policy_via_omnigent(
    *,
    server_client: httpx.AsyncClient,
    harness_client: httpx.AsyncClient,
    conversation_id: str,
    evaluation_id: str,
    phase: str,
    data: dict[str, Any],
) -> None:
    """
    Proxy a policy evaluation request from the harness to the Omnigent server.

    Called by the runner's ``proxy_stream`` when it intercepts a
    ``policy_evaluation.requested`` SSE event from the harness. Posts
    the evaluation request to the Omnigent server's
    ``POST /sessions/{id}/policies/evaluate`` endpoint, then delivers
    the verdict back to the harness as a ``policy_verdict`` inbound
    event.

    On any failure (AP unreachable, malformed response), defaults to
    ``POLICY_ACTION_ALLOW`` so a transient Omnigent outage does not block
    the agent's LLM call — fail-open is appropriate here because the
    Omnigent server's own enforcement sites (REQUEST, TOOL_CALL, etc.)
    provide the authoritative blocking gate.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param harness_client: HTTP client pointed at the harness subprocess.
    :param conversation_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param evaluation_id: Unique correlation id from the harness,
        e.g. ``"poleval_abc123"``.
    :param phase: Proto-style phase string, e.g.
        ``"PHASE_LLM_REQUEST"``.
    :param data: Event data dict for the policy engine.
    """
    # Default verdict: allow. Used on error paths.
    verdict_action = "POLICY_ACTION_ALLOW"
    verdict_reason: str | None = None
    verdict_data: dict[str, Any] | None = None

    try:
        ap_resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies/evaluate",
            json={
                "event": {
                    "type": phase,
                    "data": data,
                },
            },
            timeout=30.0,
        )
        if ap_resp.status_code == 200:
            result = ap_resp.json()
            verdict_action = result.get("result", "POLICY_ACTION_ALLOW")
            verdict_reason = result.get("reason")
            verdict_data = result.get("data")
        else:
            _logger.warning(
                "AP policy evaluate returned %d for %s; defaulting to ALLOW",
                ap_resp.status_code,
                evaluation_id,
            )
    except Exception:  # noqa: BLE001 — fail-open on Omnigent errors
        _logger.warning(
            "AP policy evaluate failed for %s; defaulting to ALLOW",
            evaluation_id,
            exc_info=True,
        )

    # Post the verdict back to the harness as a policy_verdict event.
    try:
        verdict_body: dict[str, Any] = {
            "type": "policy_verdict",
            "evaluation_id": evaluation_id,
            "action": verdict_action,
        }
        if verdict_reason is not None:
            verdict_body["reason"] = verdict_reason
        if verdict_data is not None:
            verdict_body["data"] = verdict_data
        await harness_client.post(
            f"/v1/sessions/{conversation_id}/events",
            json=verdict_body,
            timeout=30.0,
        )
    except Exception:  # noqa: BLE001 — best-effort delivery
        _logger.warning(
            "Failed to deliver policy verdict %s to harness",
            evaluation_id,
            exc_info=True,
        )

