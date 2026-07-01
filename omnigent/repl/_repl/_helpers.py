"""Rich-based REPL for omnigent — built on the UI SDK framework.

The public API is ``run_repl(client, agent_name, tool_handler)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import inspect
import json
import logging
import os
import pathlib
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TextIO

from omnigent_client import (
    BlockContext,
    ElicitationRequestCtx,
    OmnigentClient,
    OmnigentError,
    ReasoningBlock,
    ResponseEndBlock,
    ResponseStartBlock,
    Session,
    StreamHooks,
    ToolExecution,
    ToolGroup,
    ToolHandler,
    ToolResultBlock,
    format_tool_args_brief,
)
from omnigent_ui_sdk import (
    DEFAULT_USER_CONFIG,
    OverlayTarget,
    PendingAttachment,
    RichBlockFormatter,
    TerminalHost,
    TerminalTheme,
    UserConfigError,
    load_user_config,
    save_user_config,
    update_user_config,
)

# ``FormattedItem`` is the SDK formatter's per-method return type
# (``Rich.RenderableType | StreamingText | StreamReplace``). The
# top-level package doesn't re-export it today, so import from the
# internal ``_formatter`` module — keeping the import explicit
# rather than retyping every formatter override as ``list[Any]``.
# When the SDK adds an explicit re-export this should switch to
# ``from omnigent_ui_sdk import FormattedItem``.
from omnigent_ui_sdk.terminal._completer import FileMentionCompleter
from omnigent_ui_sdk.terminal._formatter import FormattedItem
from omnigent_ui_sdk.terminal._theme import LIGHT_THEME, get_theme
from prompt_toolkit.completion import CompleteEvent, Completer, Completion, merge_completers
from prompt_toolkit.document import Document
from rich.console import RenderableType
from rich.text import Text

from omnigent.spec.types import SkillSpec

if TYPE_CHECKING:
    from omnigent.server.schemas import SessionStatusEvent

_log = logging.getLogger(__name__)


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _is_recoverable_sse_transport_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a transient SSE transport interruption.

    The REPL's persistent ``_stream_pump`` auto-reconnects on every
    exception. Some of those reconnects are normal background events:
    the peer closes a long-running chunked response (load-balancer
    idle-timeout, server restart, network blip), and the next
    subscription picks up the session on the server side without any
    user-visible impact. Logging those at WARNING level alarms users
    even though nothing is wrong, and confuses the
    transient transport interruption with the genuinely-bad provider
    error (orphaned ``function_call_output`` after compression) that
    actually kills a turn. Classifying the transport errors here lets
    us demote those to INFO while keeping a clear WARNING for anything
    we don't recognise.

    :param exc: The exception caught by ``_stream_pump``.
    :returns: ``True`` when *exc* (or any wrapped cause) is a known
        recoverable httpx / httpcore transport error.
    """
    # Import lazily — ``httpx`` is a project dependency, but keeping
    # the import local avoids forcing it on minimal test environments
    # that import this module without exercising the SSE pump.
    try:
        import httpcore
        import httpx
    except ImportError:
        return False
    recoverable_types: tuple[type[BaseException], ...] = (
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpcore.RemoteProtocolError,
        httpcore.ReadError,
        httpcore.ReadTimeout,
        httpcore.ConnectError,
        httpcore.ConnectTimeout,
    )
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, recoverable_types):
            return True
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return False

class _SessionSnapshot(Protocol):
    """
    Minimal snapshot shape returned by ``client.sessions``.

    :param agent_id: Durable agent id, e.g. ``"ag_abc123"``.
    :param agent_name: Human-readable name of the bound agent,
        e.g. ``"polly"``. Changes when the session is switched
        in place to a different agent; ``None`` when the server
        couldn't resolve the agent row (or an old server omits it).
    :param runner_id: Bound runner id, e.g. ``"runner_abc123"``,
        or ``None`` before binding.
    :param reasoning_effort: Session-level reasoning effort,
        e.g. ``"high"``, or ``None`` for the agent default.
    :param llm_model: LLM model identifier from the agent spec,
        e.g. ``"anthropic/claude-sonnet-4-6"``, or ``None`` when
        unavailable.
    :param context_window: Context window size in tokens looked up
        server-side, e.g. ``200_000``, or ``None`` when unknown.
    :param last_total_tokens: Provider-reported total tokens (input +
        output) from the most recently completed task, e.g. ``45231``,
        or ``None`` when no task has completed yet. Used to seed the
        context-ring on resume without waiting for the first response.
    """

    agent_id: str
    agent_name: str | None
    runner_id: str | None
    reasoning_effort: str | None
    llm_model: str | None
    context_window: int | None
    last_total_tokens: int | None

def _session_readout_harness(session: Session) -> str:
    """Resolve the harness the ``/model`` readout should describe.

    Prefers the session's actual bound harness
    (:attr:`SessionResponse.harness`, threaded through the client into
    ``session.harness``) so the readout reflects the real provider family —
    anthropic for claude-sdk, openai for codex / openai-agents. Falls back
    to inferring from the active model string (:func:`_model_readout_harness`)
    only when the server reported no harness (older sessions / not yet
    hydrated). Inference is unreliable when the agent declares no model (a
    generic-provider launcher), which is exactly when it wrongly defaulted
    to claude-sdk and reported the anthropic family for an openai-agents run.

    :param session: The REPL session.
    :returns: A canonical harness name, e.g. ``"openai-agents"`` or
        ``"claude-sdk"``.
    """
    harness = getattr(session, "harness", None)
    if harness:
        return harness
    return _model_readout_harness(
        getattr(session, "model_override", None) or getattr(session, "llm_model", None)
    )

def _match_configured_provider(config: dict[str, object], token: str) -> str | None:
    """Resolve *token* to a configured provider name, or ``None``.

    Matches case-insensitively against both the raw provider keys and
    their friendly display names (so a user can type what the readout
    shows — ``"Anthropic"`` resolves to the configured ``"anthropic"``).
    Used by ``/model`` to detect (and reject) cross-provider switch
    attempts and to resolve a bare provider name to its default model.

    :param config: The parsed effective config mapping (``providers:``
        block).
    :param token: The user-typed token, e.g. ``"Anthropic"``,
        ``"anthropic"``, or a bare model name like ``"claude-opus-4-1"``.
    :returns: The canonical configured provider name (e.g.
        ``"anthropic"``) when *token* names one, else ``None`` (the token
        is a model name, not a provider).
    """
    from omnigent.onboarding.configure_models import provider_display_name
    from omnigent.onboarding.provider_config import load_providers

    low = token.lower()
    for name in load_providers(config):
        if name.lower() == low or provider_display_name(name).lower() == low:
            return name
    return None

def _build_github_issue_url(
    session_id: str | None,
    agent_name: str,
    description: str,
    version: str | None = None,
    os_info: str | None = None,
) -> str:
    """Build a pre-filled GitHub new-issue URL for bug reports."""
    import datetime
    from urllib.parse import quote

    session_line = f"`{session_id}`" if session_id else "not started"
    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    what_happened = description if description else "<!-- Describe what went wrong -->"

    info_lines = [
        f"- **Session ID:** {session_line}",
        f"- **Agent:** {agent_name}",
    ]
    if version:
        info_lines.append(f"- **Version:** {version}")
    if os_info:
        info_lines.append(f"- **OS:** {os_info}")
    info_lines.append(f"- **Timestamp:** {timestamp}")

    body_parts: list[str] = [
        "<!-- Filed via /report in the TUI -->",
        "",
        "## Session Info",
        "",
        *info_lines,
        "",
        "## What happened",
        "",
        what_happened,
        "",
        "## Expected behavior",
        "",
        "<!-- What did you expect? -->",
        "",
        "## Screenshots",
        "",
        "<!-- Paste a screenshot here (GitHub uploads automatically on paste) -->",
        "",
        "## Console / terminal output",
        "",
        "<!-- Scroll up in your terminal for error output, or run with",
        "     --debug-events for a JSONL event log in ~/.omnigent/debug/ -->",
    ]

    body = "\n".join(body_parts)
    base = "https://github.com/omnigent-ai/omnigent/issues/new"
    return (
        f"{base}"
        f"?title={quote('[Bug] TUI issue')}"
        f"&body={quote(body)}"
        f"&labels={quote('bug,area/harnesses')}"
    )

async def _collect_terminals_for_conversations(
    client: OmnigentClient,
    conv_ids: list[str],
    *,
    seed_items: dict[str, list[dict[str, object]]] | None = None,
) -> list[_TerminalInfo]:
    """
    Walk every named conversation's items and aggregate live terminals.

    Fetches each ``conv_id``'s items in parallel and runs
    :func:`_reconstruct_terminals_from_items` over each, then
    flattens. Sub-agents own their own conversation (via
    ``sys_session_send``) and can launch terminals there, so
    surfacing terminals across sub-agents AND the main
    conversation is what makes "I supervise a fleet of
    sub-agents each with terminals" actually visible in the
    overlay.

    :param client: The omnigent HTTP client.
    :param conv_ids: Conversations to walk. Order is preserved
        in the output so the sidebar lists main-conversation
        terminals before sub-agent ones.
    :param seed_items: Optional pre-fetched items per
        conversation, used to skip a redundant round-trip
        when the caller already has the main conversation's
        items in hand.
    :returns: All live terminals across the named
        conversations, in (conversation order, launch order)
        priority.
    """
    seed = seed_items or {}

    async def fetch_items(cid: str) -> tuple[str, list[dict[str, object]]]:
        if cid in seed:
            return cid, seed[cid]
        # Paginate past the per-request 100-item cap — same
        # reason as the parent-conversation fetch above. A
        # sub-agent conversation that ran 50+ tool calls
        # would otherwise hide its later terminals from the
        # sidebar.
        return cid, await _list_all_conversation_items(
            client,
            cid,
        )

    results = await asyncio.gather(*(fetch_items(cid) for cid in conv_ids))

    flat: list[_TerminalInfo] = []
    for cid, items in results:
        flat.extend(_reconstruct_terminals_from_items(items, conv_id=cid))
    return flat

def _tool_metadata_from_function_call_item(
    item: dict[str, object],
) -> tuple[str | None, dict[str, object] | None]:
    """Extract tool name/arguments from a ``function_call`` item.

    Sessions-API live events may surface persisted conversation items
    in either the flat API shape (``name`` / ``arguments`` at top
    level) or the entity-shaped envelope (``data.name`` /
    ``data.arguments``). The history renderer and the live
    sessions renderer both need the same tolerant extraction so tool
    result panels can dispatch to the pretty renderers instead of
    falling back to generic ``?`` JSON boxes.
    """
    name = item.get("name")
    raw_arguments = item.get("arguments")
    data = item.get("data")
    if isinstance(data, dict):
        if not isinstance(name, str):
            name = data.get("name")
        if raw_arguments is None:
            raw_arguments = data.get("arguments")
    parsed_arguments = _coerce_arguments_dict(raw_arguments)
    return (name if isinstance(name, str) else None), parsed_arguments

def _build_call_id_to_tool_metadata_lookup(
    items: list[dict[str, object]],
) -> dict[str, tuple[str, dict[str, object]]]:
    """Index ``function_call`` items' tool metadata by ``call_id``.

    ``function_call_output`` items only carry the ``call_id``;
    re-rendering them with a tool name and original arguments attached
    requires walking the full item list once and stashing each
    function_call's metadata. The result lets
    :func:`_render_history_item` call the same pretty tool renderers
    that live responses use (for example shell/read/edit panels).
    """
    out: dict[str, tuple[str, dict[str, object]]] = {}
    for item in items:
        if item.get("type") != "function_call":
            continue
        call_id = item.get("call_id")
        name, arguments = _tool_metadata_from_function_call_item(item)
        if isinstance(call_id, str) and name is not None and arguments is not None:
            out[call_id] = (name, arguments)
    return out

def _build_call_id_to_name_lookup(items: list[dict[str, object]]) -> dict[str, str]:
    """
    Index ``function_call`` items' tool names by ``call_id``.

    ``function_call_output`` items only carry the ``call_id``;
    re-rendering them with a tool name attached requires walking
    the full item list once and stashing each function_call's
    ``name`` keyed by its ``call_id``. The result is consumed by
    :func:`_render_history_item` to build the panel title for
    the matching output.

    :param items: Conversation items in any order. Both API and
        entity shapes are tolerated since each function_call
        carries the same flat ``call_id`` + ``name`` fields in
        either shape.
    :returns: Map from ``call_id`` to tool name. Items missing
        either field are skipped silently — callers fall back to
        a placeholder when a lookup misses.
    """
    return {
        call_id: name
        for call_id, (name, _arguments) in _build_call_id_to_tool_metadata_lookup(items).items()
    }

def _coerce_arguments_dict(raw: object) -> dict[str, object]:
    """
    Normalize a ``function_call.arguments`` field to a dict.

    The omnigent API surfaces tool-call arguments as a JSON
    object dict; some legacy / harness paths persist the raw
    JSON string instead. Accept both so resume rendering works
    regardless of which writer produced the row.

    :param raw: Either a dict, a JSON-encoded string, or
        anything else (returned as the empty dict).
    :returns: Parsed arguments dict, e.g. ``{"file_path": "/x.py"}``.
        Empty dict on any decode failure or non-object payload —
        the caller renders ``⏵ name()`` instead of raising.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _adapter as _sib_adapter
    from . import _approval as _sib_approval
    from . import _commands as _sib_commands
    from . import _context as _sib_context
    from . import _entry as _sib_entry
    from . import _model as _sib_model
    from . import _overview as _sib_overview
    from . import _render as _sib_render
    from . import _startup as _sib_startup
    for _key, _value in _sib_adapter.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_approval.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_commands.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_context.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_model.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_overview.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_render.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_startup.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
