"""Native Codex TUI wrapper for the Omnigent CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import click
import httpx
import yaml

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import (
    CODEX_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY
from omnigent.claude_native import (
    _attach_with_reconnect,
    attach_local_terminal,
)
from omnigent.claude_native_bridge import url_component
from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
    build_codex_native_server,
    build_codex_remote_args,
    client_for_transport,
    codex_session_meta_model_provider,
    codex_terminal_env,
    preload_codex_thread_for_resume,
    resolve_native_codex_launch,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
    bridge_dir_for_bridge_id,
    clear_bridge_state,
    codex_home_for_bridge_dir,
    prepare_bridge_dir,
    read_bridge_state,
    socket_path_for_bridge_dir,
    write_bridge_state,
)
from omnigent.codex_native_forwarder import supervise_forwarder
from omnigent.codex_native_state import read_launch_state, write_launch_state
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
    error_text,
    launch_or_reuse_daemon_runner,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native_terminal import (
    DAEMON_HOST_ONLINE_TIMEOUT_S as _DAEMON_HOST_ONLINE_TIMEOUT_S,
)
from omnigent.native_terminal import (
    DAEMON_RUNNER_ONLINE_TIMEOUT_S as _DAEMON_RUNNER_ONLINE_TIMEOUT_S,
)
from omnigent.native_terminal import (
    DAEMON_TERMINAL_READY_TIMEOUT_S as _DAEMON_TERMINAL_READY_TIMEOUT_S,
)
from omnigent.native_terminal import (
    bind_session_runner as _bind_session_runner,
)
from omnigent.native_terminal import (
    terminal_attach_url as _attach_url,
)

_logger = logging.getLogger(__name__)
from ._constants import *


