"""Forward Codex app-server notifications into Omnigent sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omnigent._native_post_delivery import post_may_have_been_delivered
from omnigent.claude_native_bridge import url_component
from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexMessage,
    client_for_transport,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
    clear_active_turn_id_if_matches,
    codex_home_for_bridge_dir,
    read_bridge_state,
    read_codex_config_model,
    update_active_turn_id,
    update_thread_id,
    write_bridge_state,
)
from omnigent.codex_native_elicitation import (
    codex_elicitation_id,
)
from omnigent.codex_native_elicitation import (
    is_codex_request_id as _is_codex_request_id,
)
from omnigent.entities.session_resources import terminal_resource_id

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

from . import _turn as _boot_turn

def _promote_bootstrap_bindings() -> None:
    g = globals()
    for _key, _value in _boot_turn.__dict__.items():
        if _key == '_command_execution_tool_call':
            g[_key] = _value
    for _key, _value in _boot_turn.__dict__.items():
        if _key == '_file_change_tool_call':
            g[_key] = _value
    for _key, _value in _boot_turn.__dict__.items():
        if _key == '_web_search_tool_call':
            g[_key] = _value


_promote_bootstrap_bindings()

_CODEX_ELICITATION_REQUEST_METHODS = frozenset(
    {
        _CODEX_MCP_ELICITATION_REQUEST_METHOD,
        _CODEX_TOOL_REQUEST_USER_INPUT_METHOD,
        _CODEX_COMMAND_EXECUTION_REQUEST_APPROVAL_METHOD,
        _CODEX_FILE_CHANGE_REQUEST_APPROVAL_METHOD,
        _CODEX_PERMISSIONS_REQUEST_APPROVAL_METHOD,
        _CODEX_EXEC_COMMAND_APPROVAL_METHOD,
        _CODEX_APPLY_PATCH_APPROVAL_METHOD,
    }
)
_TOOL_ITEM_BUILDERS: dict[str, _ToolItemBuilder] = {
    "commandExecution": _command_execution_tool_call,
    "fileChange": _file_change_tool_call,
    "webSearch": _web_search_tool_call,
}
_TOOL_ITEM_TYPES = frozenset(_TOOL_ITEM_BUILDERS)
