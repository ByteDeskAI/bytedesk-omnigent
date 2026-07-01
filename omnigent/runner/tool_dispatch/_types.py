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

class AgentSpecLike(Protocol):
    """Structural view of the runner's agent spec (sweep-2 BDP-2363).

    The dispatch sites carry the agent spec as a bare ``Any`` and read it
    defensively (``getattr(agent_spec, "x", None)``). This Protocol names the
    exact attribute surface dispatch reads, turning unchecked ``getattr`` into
    checked attribute access while staying import-free: the runner carrier
    intentionally does not import the concrete
    :class:`omnigent.spec.types.AgentSpec`, so this is declared as a
    ``Protocol`` and the real ``AgentSpec`` satisfies it structurally.

    Attribute types are deliberately loose (``Sequence``/``Any``) so the
    concrete ``AgentSpec`` (whose fields are ``list[...]`` of concrete element
    types) is a structural match, and so the element types stay unpinned —
    dispatch reads them via further ``getattr``/duck typing.
    """

    name: str | None
    tools: Any
    skills: Sequence[Any]
    skills_filter: str | list[str]
    mcp_servers: Sequence[Any]
    local_tools: Sequence[Any]
    sub_agents: Sequence[Any]
    executor: Any
    os_env: Any

class ActionRequiredItem(TypedDict, total=False):
    """The ``item`` body of an ``action_required`` SSE event.

    The runner inspects this dict on every relayed
    ``response.output_item.done`` event to decide whether a tool call must
    be dispatched locally. ``total=False`` because the runner reads each
    key defensively (``.get(...)``) and a non-tool item omits the
    tool-call fields.

    :param type: Item discriminator; ``"function_call"`` for a tool call.
    :param status: Lifecycle status; ``"action_required"`` when the
        runner must dispatch the call.
    :param name: The tool name, e.g. ``"sys_os_shell"``.
    :param call_id: Correlation id echoed back on the result.
    :param arguments: Tool arguments as a JSON string.
    """

    type: str
    status: str
    name: str
    call_id: str
    arguments: str

class ActionRequiredEvent(TypedDict, total=False):
    """An SSE event carrying a possible ``action_required`` tool call.

    The exact dict shape :func:`is_action_required` / :func:`get_tool_name`
    / :func:`get_call_id` / :func:`get_arguments` already read. ``item`` is
    absent on non-output events, so the keys stay optional.

    :param type: Event type; ``"response.output_item.done"`` carries an item.
    :param item: The output item (see :class:`ActionRequiredItem`).
    """

    type: str
    item: ActionRequiredItem

class SubagentSendArgs(TypedDict, total=False):
    """Parsed LLM arguments at the ``sys_session_send`` dispatch boundary.

    Names the keys :func:`_execute_subagent_tool` reads from the parsed
    tool-call args. PRESERVES the loud-vs-soft asymmetry the dispatcher
    enforces at runtime — this TypedDict only documents the surface, it
    does not validate it:

    - a malformed ``args.model`` fails **loud** (raises ``ValueError`` in
      :func:`_subagent_model_from_args`, surfaced as an ``Error:`` reply);
    - a malformed message / addressing mode fails **soft** (a ``None``
      message yields an ``Error:`` string return without raising).

    ``total=False`` because exactly one addressing mode is supplied
    (``session_id`` XOR ``agent`` + ``title``) and ``args`` may be a bare
    string or the object form.

    :param agent: Named sub-agent to spawn/continue, e.g. ``"researcher"``.
    :param title: Sub-agent instance label (named mode), e.g. ``"auth"``.
    :param session_id: Existing child session id (by-session-id mode).
    :param args: Either the raw message string, or the object form
        ``{"input": <msg>, "purpose"?: <str>, "model"?: <str>}``.
    """

    agent: str
    title: str
    session_id: str
    args: str | SubagentSendArgsObject

class SubagentSendArgsObject(TypedDict, total=False):
    """The object form of ``SubagentSendArgs.args``.

    :param input: The user message text for the sub-agent.
    :param purpose: Optional classification hint (e.g. polly's guardrail).
    :param model: Optional per-dispatch model override id.
    """

    input: str
    purpose: str
    model: str

class SubagentInboxPayload(TypedDict):
    """Terminal sub-agent completion pushed into the parent session inbox.

    The exact dict :func:`_deliver_subagent_completion` (``omnigent.runner.app``)
    enqueues onto the parent's ``sys_read_inbox`` queue. Named here so the
    inbox producer and the drain side share one contract.

    :param type: Always ``"sub_agent"``.
    :param work_id: Runner-local work entry id.
    :param task_id: Child session id (legacy alias).
    :param handle_id: Child session id (legacy alias).
    :param conversation_id: Child session id.
    :param tool_name: Dispatching sub-agent name.
    :param agent: Dispatching sub-agent name.
    :param title: Sub-agent instance title.
    :param status: Terminal status string (``"completed"`` / ``"failed"`` /
        ``"cancelled"``).
    :param output: Sub-agent output text (a placeholder when empty).
    """

    type: str
    work_id: str
    task_id: str
    handle_id: str
    conversation_id: str
    tool_name: str
    agent: str
    title: str | None
    status: str
    output: str

class SessionSnapshotPayload(TypedDict, total=False):
    """The ``GET /v1/sessions/{id}`` JSON body the runner projects.

    Names the subset of the server :class:`~omnigent.server.schemas.SessionResponse`
    body that the runner's single-flight ``_session_snapshot`` loader reads
    (``omnigent.runner.app``). ``total=False`` + nullable members mirror the
    runner's defensive ``body.get(...)`` reads: a not-yet-bound session omits
    / nulls ``agent_id``, and ``sub_agent_name`` is present only on sub-agent
    sessions.

    :param created_at: Server creation time (UNIX seconds).
    :param workspace: Server-stored workspace path, or ``None``.
    :param agent_id: Bound agent id, or ``None`` before binding.
    :param sub_agent_name: Dispatched sub-agent name on sub-agent sessions,
        else ``None``.
    """

    created_at: float
    workspace: str | None
    agent_id: str | None
    sub_agent_name: str | None

