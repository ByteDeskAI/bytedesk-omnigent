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
_AGENT_NAME = "codex-native-ui"
_SUBSCRIBE_RETRY_DELAY_SECONDS = 0.2
_THREAD_START_TIMEOUT_SECONDS = 30.0
_NO_ROLLOUT_FRAGMENT = "no rollout found for thread id"
_EMPTY_ROLLOUT_FRAGMENT = "is empty"
_POST_MAX_ATTEMPTS = 3
_POST_RETRY_DELAY_SECONDS = 0.1
_POST_RETRY_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_DELTA_FLUSH_INTERVAL_SECONDS = 0.05
_DELTA_FLUSH_CHAR_THRESHOLD = 64
_CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS = 86405.0
_CODEX_ELICITATION_CONNECT_TIMEOUT_SECONDS = 30.0
_CODEX_ELICITATION_RETRY_INITIAL_BACKOFF_SECONDS = 1.0
_CODEX_ELICITATION_RETRY_MAX_BACKOFF_SECONDS = 30.0
_CODEX_MCP_ELICITATION_REQUEST_METHOD = "mcpServer/elicitation/request"
_CODEX_TOOL_REQUEST_USER_INPUT_METHOD = "item/tool/requestUserInput"
_CODEX_COMMAND_EXECUTION_REQUEST_APPROVAL_METHOD = "item/commandExecution/requestApproval"
_CODEX_FILE_CHANGE_REQUEST_APPROVAL_METHOD = "item/fileChange/requestApproval"
_CODEX_PERMISSIONS_REQUEST_APPROVAL_METHOD = "item/permissions/requestApproval"
_CODEX_EXEC_COMMAND_APPROVAL_METHOD = "execCommandApproval"
_CODEX_APPLY_PATCH_APPROVAL_METHOD = "applyPatchApproval"
_CODEX_SERVER_REQUEST_RESOLVED_METHOD = "serverRequest/resolved"
_EXTERNAL_SESSION_INTERRUPTED_TYPE = "external_session_interrupted"
_EXTERNAL_ELICITATION_RESOLVED_TYPE = "external_elicitation_resolved"
_CODEX_COLLAB_AGENT_ITEM_TYPE = "collabAgentToolCall"
_CODEX_COLLAB_SPAWN_TOOL = "spawnAgent"
_CODEX_COLLAB_RUNNING_STATUSES = frozenset({"pendingInit", "running"})
_CODEX_COLLAB_FAILED_STATUSES = frozenset({"errored", "notFound"})
_EXTERNAL_CODEX_SUBAGENT_START_TYPE = "external_codex_subagent_start"
_PLAN_IMPLEMENTATION_QUESTION_ID = "plan_implementation"
_PLAN_IMPLEMENTATION_TITLE = "Implement this plan?"
_PLAN_IMPLEMENTATION_YES = "Yes, implement this plan"
_PLAN_IMPLEMENTATION_CLEAR_CONTEXT = "Yes, clear context and implement"
_PLAN_IMPLEMENTATION_NO = "No, stay in Plan mode"
_PLAN_IMPLEMENTATION_CODING_MESSAGE = "Implement the plan."
_PLAN_IMPLEMENTATION_CLEAR_CONTEXT_PREFIX = (
    "A previous agent produced the plan below to accomplish the user's task. "
    "Implement the plan in a fresh context. Treat the plan as the source of "
    "user intent, re-read files as needed, and carry the work through "
    "implementation and verification."
)
_ToolItemBuilder = Callable[[str, dict[str, Any]], "_CodexToolCall | None"]

