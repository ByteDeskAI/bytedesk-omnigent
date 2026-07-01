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


SlashCommandHandler = Callable[
    [str, Session, OmnigentClient, TerminalHost, RichBlockFormatter],
    Awaitable[None],
]
WELCOME_HINTS = ["/help help", "Ctrl+O debug", "Ctrl+T show tools", "Esc cancel", "Ctrl+C exit"]
_LIST_ITEMS_PAGE_SIZE = 100
_ANSI_DIM = "\033[2m"
_ANSI_RESET = "\033[0m"
_APPROVE_ONCE_TOKENS: frozenset[str] = frozenset({"y", "yes", "approve", "ok"})
_APPROVE_ALWAYS_TOKENS: frozenset[str] = frozenset(
    {"a", "always", "yes always", "approve always"},
)
_RENDERABLE_OUTPUT_ITEM_TYPES = ("function_call", "function_call_output", "slash_command")
_THEME_CLEAR_ALIASES = {"default", "auto", "reset"}
_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_CLEAR_ALIASES = {"default", "off", "reset"}
_MODEL_CLEAR_ALIASES = {"default", "off", "reset"}
_CONTEXT_COMPACTION_TRIGGER: float = 0.8
_CONTEXT_COIN_TOTAL: int = 10  # bar width in positions
_CONTEXT_COIN_USED: str = "█"
_CONTEXT_COIN_FREE: str = "░"
_CONTEXT_COIN_BUF: str = "▓"
_TERMINAL_KEY_PREFIX = "terminal::"
_SLASH_COMMAND_ALIASES: frozenset[str] = frozenset({"/?", "/exit"})

