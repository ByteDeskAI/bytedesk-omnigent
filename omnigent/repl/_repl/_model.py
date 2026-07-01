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

async def _set_session_reasoning_effort(
    session: Session,
    effort: str | None,
) -> None:
    """
    Set reasoning effort on either legacy or sessions-backed chat.

    The legacy SDK helper exposes a synchronous
    ``set_reasoning_effort`` mutator. The sessions-backed REPL
    adapter persists via HTTP and therefore returns an awaitable.
    This adapter keeps the slash command surface shared while still
    awaiting the server-backed path.

    :param session: Current REPL session.
    :param effort: New effort, e.g. ``"high"``, or ``None`` to
        clear to the agent default.
    :returns: None.
    """
    result = session.set_reasoning_effort(effort)
    if inspect.isawaitable(result):
        await result

def _model_readout_harness(active_model: str | None) -> str:
    """Infer the harness whose active credential ``/model`` should describe.

    The REPL ``Session`` does not carry its harness name, but the active
    credential readout is per-family (anthropic vs openai). We infer the
    family from the active model string (the in-session override if set,
    else the agent's spec model) via
    :func:`omnigent.llms.routing.infer_harness_from_model`, falling back
    to ``"claude-sdk"`` (the anthropic surface) when the model is
    unrecognised — that yields the anthropic-family default, the most
    common single-key setup, rather than guessing the openai surface.

    :param active_model: The override or spec model, e.g.
        ``"openai/gpt-5.5"`` or ``"claude-sonnet-4-6"``, or ``None`` when
        neither is set.
    :returns: A canonical harness name, e.g. ``"claude-sdk"`` or
        ``"openai-agents"``.
    """
    from omnigent.llms.routing import infer_harness_from_model

    if active_model:
        inferred = infer_harness_from_model(active_model)
        if inferred:
            return inferred
    return "claude-sdk"

def _build_model_readout_lines(
    config: dict[str, object],
    harness: str,
    model_override: str | None,
) -> list[str]:
    """Build the ``/model`` (no-arg) active-credential readout lines.

    Renders one ``Active:`` line — ``<model> · <glyph friendly-provider>
    · <source>`` via :func:`describe_active_credential` — using the kind
    glyph in place of the kind word so a databricks provider named
    ``databricks`` doesn't render as the redundant ``databricks ·
    databricks``. When other providers are also configured, an ``Also
    configured:`` line lists them (friendly names + glyphs) with honest
    guidance: ``/model`` only changes the model within the active
    provider — switching the active provider mid-session is not wired, so
    it goes through ``omnigent setup --no-internal-beta`` + a restart. Falls
    back to the legacy ``(agent default)`` line when nothing is configured
    for the harness's surface.

    No ambient-shadow warning is emitted: a configured default
    (``default: true``) takes precedence over ambient env keys, so an
    ambient ``$ANTHROPIC_API_KEY`` does *not* shadow it — warning the
    opposite was misleading. (If ambient is what's actually used, no
    default is configured and the ``(agent default)`` branch is taken.)

    :param config: The parsed effective config mapping (``providers:``
        block), e.g. from
        :func:`omnigent.onboarding.provider_config.load_config`.
    :param harness: The harness whose credential to describe, e.g.
        ``"claude-sdk"``.
    :param model_override: The in-session ``/model`` override, e.g.
        ``"openai/gpt-5.5"``, or ``None``.
    :returns: Plain (un-markup) display lines, e.g.
        ``["Active:  claude-sonnet-4-6  ·  🔑 Anthropic API Key  ·  $ANTHROPIC_API_KEY",
        "Also configured:  🧱 Databricks", "  /model <name> changes the model. …"]``.
    """
    from omnigent.onboarding.configure_models import (
        credential_label,
        kind_glyph,
    )
    from omnigent.onboarding.provider_config import (
        PI_SURFACE,
        describe_active_credential,
        harness_family,
        load_providers,
        provider_families,
    )

    lines: list[str] = []
    cred = describe_active_credential(config, harness, model_override=model_override)
    if cred is None:
        # Nothing resolves for this harness's surface — not in the explicit
        # config, and nothing ambient was detected (the merged view the
        # caller passes already includes detections). Be honest: report
        # None rather than fabricate a family default. An in-session
        # override is still shown (it's real), but its provider is
        # unresolved until one is configured.
        if model_override is not None:
            lines.append(f"Active:  {model_override}  ·  (provider unresolved)")
        else:
            lines.append("Active:  None  ·  None")
            lines.append(
                "no model configured — run `omnigent setup --no-internal-beta` to add one"
            )
        lines.append("usage: /model <name> · /model default | off | reset to clear")
        return lines

    # One clean "Active" line: <model> · <glyph friendly-provider> · <source>.
    # The kind glyph stands in for the kind word, so a databricks provider
    # named "databricks" no longer renders as the redundant "databricks ·
    # databricks". A provider whose model is chosen elsewhere (databricks
    # profile, subscription CLI) gets an explicit phrase instead of a model.
    if cred.model:
        model_label = cred.model
    elif cred.kind == "databricks":
        model_label = "(Databricks profile picks the model — pin one with /model <name>)"
    elif cred.kind == "subscription":
        model_label = "(CLI login picks the model — pin one with /model <name>)"
    else:
        model_label = "(no model pinned — set one with /model <name>)"
    glyph = kind_glyph(cred.kind)
    # credential_label is the single source of truth shared with `configure
    # harnesses` — a subscription reads "Subscription" (not the brand name
    # "Claude"), a key names the vendor + "API Key", Databricks names itself.
    provider_label = f"{glyph} {credential_label(cred.kind, cred.provider_name)}".strip()
    lines.append(f"Active:  {model_label}  ·  {provider_label}  ·  {cred.source}")

    # List the OTHER configured providers that serve THIS harness's family,
    # so the user only sees relevant alternatives (a Codex run shouldn't list
    # Claude-only providers). A both-family harness (pi) maps to no single
    # family — filter its alternates on the pi surface instead, which every
    # kind but subscription serves (a CLI login can't drive pi).
    providers = load_providers(config)
    fam = harness_family(harness)
    surface = fam if fam is not None else PI_SURFACE
    others = [
        (name, entry)
        for name, entry in providers.items()
        if name != cred.provider_name and surface in provider_families(entry)
    ]
    if others:
        items = [
            (
                f"{kind_glyph(e.kind)} "
                f"{credential_label(e.kind, n, profile=e.profile, display_name=e.display_name)}"
            ).strip()
            for n, e in others
        ]
        lines.append("Also configured:  " + "  ·  ".join(items))
        # Honest guidance: `/model` only changes the model within the active
        # provider; switching the active provider mid-session is not wired,
        # so it goes through `configure harnesses` + a restart.
        lines.append(
            "  /model <name> changes the model. To switch provider: "
            "omnigent setup --no-internal-beta (then restart)."
        )
    return lines

def _resolve_provider_default_model(config: dict[str, object], provider_name: str) -> str | None:
    """Resolve a configured provider's default model for ``/model <provider>``.

    Looks up *provider_name* in the parsed providers and returns its
    family default model (anthropic preferred, else openai). Returns
    ``None`` when the provider is not configured or declares no default
    model (e.g. a bare gateway or a subscription whose CLI picks the
    model).

    :param config: The parsed effective config mapping.
    :param provider_name: The configured provider name, e.g.
        ``"anthropic"``.
    :returns: The provider's default model, e.g. ``"claude-sonnet-4-6"``,
        or ``None``.
    """
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        load_providers,
    )

    providers = load_providers(config)
    entry = providers.get(provider_name)
    if entry is None:
        return None
    for family in (ANTHROPIC_FAMILY, OPENAI_FAMILY):
        default_model = entry.family_default_model(family)
        if default_model:
            return default_model
    return None

def _model_validation_warning(model: str) -> str | None:
    """Return a warning when *model* is not in the catalog, else ``None``.

    Validates ``provider/model`` against the bundled catalog
    (:func:`omnigent.onboarding.providers.get_chat_models`). An unknown
    provider prefix or an unlisted model returns a human-readable warning
    string; ``/model`` warns but does **not** block on it (gateways and
    new models are legitimately absent from the catalog).

    :param model: The model string the user passed, e.g.
        ``"openai/gpt-5.5"`` or ``"anthropic/claude-sonnet-4-6"``.
    :returns: A warning string when the model is not found in the catalog,
        e.g. ``"'openai/ghost' is not in the model catalog (continuing
        anyway)."``, or ``None`` when it validates.
    """
    from omnigent.errors import OmnigentError
    from omnigent.llms.routing import parse_model_string
    from omnigent.onboarding.providers import get_chat_models

    try:
        routed = parse_model_string(model)
    except OmnigentError:
        # A non-catalog prefix is normal for gateway / OSS models (e.g.
        # ``qwen/qwen3.7-plus`` via OpenRouter) — the gateway, not our
        # catalog, owns the naming. Inform, don't alarm.
        return f"{model!r} isn't a catalog model — fine for gateway / OSS models; using it as-is."
    catalog_models = {m.name for m in get_chat_models(routed.provider)}
    if catalog_models and routed.model not in catalog_models:
        return (
            f"{model!r} isn't in the local model catalog (may lag new releases); using it as-is."
        )
    return None


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _adapter as _sib_adapter
    from . import _approval as _sib_approval
    from . import _commands as _sib_commands
    from . import _context as _sib_context
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
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
    for _key, _value in _sib_helpers.__dict__.items():
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
