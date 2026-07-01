"""Bridge utilities for the native Claude Code wrapper.

The native wrapper has two live processes that need to rendezvous:

- Claude Code, running in the user's terminal resource.
- The Omnigent harness turn, running when the web UI submits a
  message to the session agent.

This module owns the small filesystem rendezvous directory plus two
helper surfaces:

- An MCP stdio server (``serve-mcp`` subcommand) that Claude Code
  launches as a child process. It advertises Omnigent tools to
  Claude (workspace ``sys_os_*`` tools outside an active turn,
  active-turn Omnigent tools via a per-turn relay).
- A tmux send-keys path. Web UI messages are delivered to Claude by
  typing them into the same tmux pane the user is attached to;
  Claude treats them as ordinary user input. The runner advertises
  the pane's socket + target in ``tmux.json`` after launching the
  ``claude/main`` terminal.

Claude's experimental Channels MCP capability was the original input
path but is blocked at the org policy layer, so this bridge does not
use it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import queue
import re
import secrets
import shlex
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib import error, request

from omnigent.claude_native_message_display_hook import MESSAGE_DELTAS_FILE

if TYPE_CHECKING:
    from omnigent.llms.context_window import ModelPricing

from omnigent.inner.bundle_skills import claude_native_skill_args
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import OSEnvironment, create_os_environment
from omnigent.reasoning_effort import CLAUDE_EFFORTS
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins.os_env import build_os_env_tools

BRIDGE_DIR_ENV_VAR = "HARNESS_CLAUDE_NATIVE_BRIDGE_DIR"
REQUEST_SESSION_ID_ENV_VAR = "HARNESS_CLAUDE_NATIVE_REQUEST_SESSION_ID"
BRIDGE_ID_LABEL_KEY = "omnigent.claude_native.bridge_id"

# Root for the per-process Claude bridge tree. Namespaced by uid so
# other Unix users on the same host cannot read the bearer token or
# pre-create the parent as a symlink to redirect the bridge tree. The
# trusted parent (`/tmp`) is shared; everything under
# `_BRIDGE_ROOT_PARENT` must be owned by the current uid and not be a
# symlink — see :func:`_ensure_secure_dir`.
_TRUSTED_PARENT = Path("/tmp")
_BRIDGE_ROOT_PARENT = _TRUSTED_PARENT / f"omnigent-{os.getuid()}"
_BRIDGE_ROOT = _BRIDGE_ROOT_PARENT / "claude-native"
_CONFIG_FILE = "bridge.json"
_SERVER_FILE = "server.json"
_STATE_FILE = "state.json"
_HOOKS_FILE = "hooks.jsonl"
_RECENT_LOCAL_COMMAND_LINE_LIMIT = 200
_RECENT_LOCAL_COMMAND_WINDOW_S = 10.0
_FORKED_FROM_LINE_LIMIT = 200
_TOOL_RELAY_FILE = "tool_relay.json"
_TMUX_FILE = "tmux.json"
_PERMISSION_HOOK_FILE = "permission_hook.json"
_CONTEXT_FILE = "context.json"
_USER_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_MCP_SERVER_NAME = "omnigent"
_MCP_PROTOCOL_VERSION = "2024-11-05"
# Tools-changed: harness POSTs to the bridge MCP server's localhost
# control endpoint, which emits ``notifications/tools/list_changed``
# on its MCP stdout. Standard MCP notification — unrelated to the
# experimental Claude Channels feature that this module no longer
# uses.
_TOOLS_CHANGED_READY_TIMEOUT_S = 30.0
_TOOLS_CHANGED_POST_TIMEOUT_S = 10.0
# Ceiling the relay HTTP handler (``_run_relay_tool``) waits for a single
# tool dispatch to complete on the harness event loop.
_TOOL_CALL_TIMEOUT_S = 300.0
# Timeout for the bridge's POST to the active-turn relay server
# (``_call_relay_tool``). This is the OUTER hop: it waits for the relay
# handler's entire ``_TOOL_CALL_TIMEOUT_S`` dispatch, which itself fans out
# to the Omnigent policy server and back. It MUST exceed ``_TOOL_CALL_TIMEOUT_S``
# so the inner handler times out first and returns a clean MCP error over
# HTTP 200 — rather than the outer ``urlopen`` raising and tearing down the
# stdio MCP server (see ``_stdio_jsonrpc_loop``). The previous flat 10s sat
# below the real round-trip latency under load, so slow-but-healthy calls
# (session history reads, shell) tripped it and crashed the bridge.
_TOOL_RELAY_POST_TIMEOUT_S = _TOOL_CALL_TIMEOUT_S + 30.0
# Web-UI → Claude input now flows through tmux send-keys, not
# Claude's experimental Channels MCP capability. The runner writes
# ``tmux.json`` after the Claude terminal launches; the harness
# tails it and shells out to tmux.
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 5.0
# Claude Code renders this prompt glyph in its input box once the TUI
# is interactive. We poll ``capture-pane`` for it before injecting the
# first message so keystrokes typed during Claude's boot aren't dropped.
# The glyph persists while Claude is busy responding, so its presence
# means "input box mounted" (not "idle"), which is what injection needs.
_CLAUDE_PROMPT_GLYPH = "❯"
# How many trailing non-empty lines to scan for the prompt glyph. The
# input box sits near the bottom of the pane; scanning only the tail
# avoids false positives from the glyph appearing in scrollback output.
# The window has to clear the footer rendered below the box — some
# people's statuslines run ~3 lines — so the ``❯`` row isn't the last
# non-empty line.
_PROMPT_SCAN_TAIL_LINES = 5
_CLAUDE_READY_POLL_INTERVAL_S = 0.15
_PASTE_SETTLE_S = 0.1  # let the TUI commit a paste before the separate submit Enter
# How long to wait for the pasted draft to visibly land in Claude's
# input box before sending the submit Enter. Claude Code coalesces
# rapid stdin bursts into a paste, so an Enter sent while the TUI is
# still consuming the paste gets folded in as a newline instead of
# submitting — the draft then sits unsent. Polling for the draft makes
# the handoff deterministic where the old fixed sleep raced it.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# After the submit Enter, how long to keep checking that the draft
# actually left the input box (re-sending Enter while it hasn't)
# before failing loud.
_SUBMIT_VERIFY_TIMEOUT_S = 10.0
# Minimum spacing between repeated submit Enters during verification.
# Long enough for the TUI to clear the box after a successful submit
# (so a slow-but-successful first Enter isn't double-tapped), short
# enough that a swallowed Enter is retried promptly.
_SUBMIT_RETRY_INTERVAL_S = 1.0
# Claude Code collapses large pastes into this placeholder in the
# input box instead of rendering the text itself.
_PASTED_PLACEHOLDER_PREFIX = "[Pasted text"
# How many characters of the draft's first line to use when checking
# whether the draft is rendered in the input box. Short enough to fit
# on the prompt row of a default 80-column detached pane.
_DRAFT_NEEDLE_MAX_CHARS = 24

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def start_tool_relay(
    *,
    bridge_dir: Path,
    tools: list[dict[str, Any]],
    tool_executor: ToolExecutor,
    loop: asyncio.AbstractEventLoop,
) -> ClaudeNativeToolRelay:
    """
    Start a relay for Omnigent tool calls from Claude.

    Writes ``tool_relay.json`` and starts the localhost HTTP server that
    backs it. The caller owns the relay's lifetime (a single turn or a
    whole session) and must call :meth:`ClaudeNativeToolRelay.close` when
    that scope ends.

    :param bridge_dir: Bridge directory path.
    :param tools: Omnigent tool schemas to advertise, e.g.
        ``[{"name": "sys_os_read", "parameters": {...}}]``.
    :param tool_executor: Callback used to dispatch one tool call through
        AP/runner.
    :param loop: Event loop that owns ``tool_executor``.
    :returns: Started relay handle. Call :meth:`close` when the relay's
        scope ends (e.g. on session delete).
    """
    token = secrets.token_urlsafe(32)
    handler_cls = _tool_relay_handler_factory(token, tool_executor, loop)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = httpd.server_address
    relay_info = {
        "url": f"http://{host}:{port}",
        "token": token,
        "tools": _normalize_relay_tool_specs(tools),
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    _write_json_file(bridge_dir / _TOOL_RELAY_FILE, relay_info)
    thread = threading.Thread(
        target=httpd.serve_forever,
        name="claude-native-tool-relay",
        daemon=True,
    )
    thread.start()
    return ClaudeNativeToolRelay(bridge_dir=bridge_dir, httpd=httpd)

def main(argv: list[str] | None = None) -> None:
    """
    CLI entrypoint for bridge helper processes.

    :param argv: Optional argv override excluding program name.
        ``None`` reads :data:`sys.argv`.
    :returns: None.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "serve-mcp":
        _serve_mcp(Path(args.bridge_dir))
        return
    raise SystemExit(f"unknown command: {args.command}")

def _parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse bridge helper CLI arguments.

    :param argv: CLI argv excluding program name, e.g.
        ``["serve-mcp", "--bridge-dir", "/tmp/x"]``.
    :returns: Parsed argparse namespace.
    """
    parser = argparse.ArgumentParser(prog="python -m omnigent.claude_native_bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve-mcp")
    serve.add_argument("--bridge-dir", required=True)
    return parser.parse_args(argv)

def _serve_mcp(bridge_dir: Path) -> None:
    """
    Run the MCP stdio server and the local control HTTP endpoint.

    :param bridge_dir: Bridge directory path.
    :returns: None when stdin closes.
    """
    os.environ[BRIDGE_DIR_ENV_VAR] = str(bridge_dir)
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not isinstance(config, dict):
        raise SystemExit(f"bridge config missing: {bridge_dir / _CONFIG_FILE}")
    token = config.get("token")
    if not isinstance(token, str) or not token:
        raise SystemExit("bridge config missing token")

    notification_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stdout_lock = threading.Lock()
    httpd = _start_http_ingress(bridge_dir, token, notification_queue)
    tools, close_tools = _build_tools(config)
    writer = threading.Thread(
        target=_notification_writer,
        args=(notification_queue, stdout_lock),
        name="claude-native-mcp-writer",
        daemon=True,
    )
    writer.start()
    try:
        _stdio_jsonrpc_loop(tools, stdout_lock, bridge_dir)
    finally:
        notification_queue.put(None)
        httpd.shutdown()
        httpd.server_close()
        close_tools()

def _start_http_ingress(
    bridge_dir: Path,
    token: str,
    notification_queue: queue.Queue[dict[str, Any] | None],
) -> ThreadingHTTPServer:
    """
    Start the localhost control HTTP server.

    Currently only serves ``POST /tools-changed``, which queues a
    standard MCP ``notifications/tools/list_changed`` for the stdio
    writer to emit.

    :param bridge_dir: Bridge directory path.
    :param token: Bearer token used for local requests.
    :param notification_queue: Queue consumed by the MCP stdout
        writer thread.
    :returns: Started :class:`ThreadingHTTPServer`.
    """
    handler_cls = _handler_factory(token, notification_queue)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = httpd.server_address
    server_info = {
        "url": f"http://{host}:{port}",
        "token": token,
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    _write_json_file(bridge_dir / _SERVER_FILE, server_info)
    thread = threading.Thread(
        target=httpd.serve_forever,
        name="claude-native-mcp-http",
        daemon=True,
    )
    thread.start()
    return httpd

def _handler_factory(
    token: str,
    notification_queue: queue.Queue[dict[str, Any] | None],
) -> type[BaseHTTPRequestHandler]:
    """
    Create an HTTP handler class bound to the MCP notification queue.

    :param token: Bearer token expected in ``Authorization``.
    :param notification_queue: Queue receiving MCP notification
        payloads.
    :returns: A concrete :class:`BaseHTTPRequestHandler` subclass.
    """

    class _ControlHandler(BaseHTTPRequestHandler):
        """HTTP handler for the local MCP control endpoint."""

        def log_message(self, format: str, *args: Any) -> None:
            """
            Suppress default HTTP server logging.

            :param format: Log format string from
                :class:`BaseHTTPRequestHandler`.
            :param args: Format arguments.
            :returns: None.
            """
            del format, args

        def do_GET(self) -> None:
            """
            Serve the local health endpoint.

            :returns: None.
            """
            if self.path != "/health":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_json({"status": "ok"})

        def do_POST(self) -> None:
            """
            Accept the local MCP control POST for tools/list_changed.

            :returns: None.
            """
            if self.path != "/tools-changed":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_error(HTTPStatus.UNAUTHORIZED)
                return
            notification_queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                    "params": {},
                }
            )
            self._send_json({"ok": True})

        def _send_json(self, payload: dict[str, Any]) -> None:
            """
            Send a JSON response body.

            :param payload: JSON-compatible response object.
            :returns: None.
            """
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return _ControlHandler

def _tool_relay_handler_factory(
    token: str,
    tool_executor: ToolExecutor,
    loop: asyncio.AbstractEventLoop,
) -> type[BaseHTTPRequestHandler]:
    """
    Create an HTTP handler class for active-turn tool calls.

    :param token: Bearer token expected in ``Authorization``.
    :param tool_executor: Existing harness callback used to
        dispatch one tool call.
    :param loop: Event loop that owns ``tool_executor``.
    :returns: A concrete :class:`BaseHTTPRequestHandler` subclass.
    """

    class _ToolRelayHandler(BaseHTTPRequestHandler):
        """HTTP handler for active Omnigent tool relay calls."""

        def log_message(self, format: str, *args: Any) -> None:
            """
            Suppress default HTTP server logging.

            :param format: Log format string from
                :class:`BaseHTTPRequestHandler`.
            :param args: Format arguments.
            :returns: None.
            """
            del format, args

        def do_POST(self) -> None:
            """
            Accept one MCP tool call from the Claude helper process.

            :returns: None.
            """
            if self.path != "/tool":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_error(HTTPStatus.UNAUTHORIZED)
                return
            payload = self._read_json_body()
            if payload is None:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            name = payload.get("name")
            arguments = payload.get("arguments")
            if not isinstance(name, str) or not name:
                self._send_json(_mcp_error("tool relay request missing name"))
                return
            if not isinstance(arguments, dict):
                arguments = {}
            self._send_json(_run_relay_tool(tool_executor, loop, name, arguments))

        def _read_json_body(self) -> dict[str, Any] | None:
            """
            Read and decode a JSON request body.

            :returns: Parsed JSON object, or ``None`` when the body
                is malformed.
            """
            length_raw = self.headers.get("Content-Length", "0")
            try:
                length = int(length_raw)
            except ValueError:
                return None
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None

        def _send_json(self, payload: dict[str, Any]) -> None:
            """
            Send a JSON response body.

            :param payload: JSON-compatible response object.
            :returns: None.
            """
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return _ToolRelayHandler

def _run_relay_tool(
    tool_executor: ToolExecutor,
    loop: asyncio.AbstractEventLoop,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute one relay tool call on the harness event loop.

    :param tool_executor: Existing harness callback used to
        dispatch one tool call.
    :param loop: Event loop that owns ``tool_executor``.
    :param name: Tool name, e.g. ``"sys_os_shell"``.
    :param arguments: Decoded tool arguments.
    :returns: MCP tool-call response.
    """
    future = asyncio.run_coroutine_threadsafe(tool_executor(name, arguments), loop)
    try:
        result = future.result(timeout=_TOOL_CALL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - relay converts callback failures to MCP errors.
        return _mcp_error(f"Omnigent tool dispatch failed: {exc}")
    return _mcp_response_from_tool_result(result)

def _mcp_response_from_tool_result(result: Any) -> dict[str, Any]:
    """
    Convert a harness tool result into MCP response shape.

    :param result: Result returned by ``_tool_executor``. Existing
        harnesses usually return a dict, e.g. ``{"result": "ok"}``.
    :returns: MCP tool-call response.
    """
    payload = result if isinstance(result, dict) else {"result": result}
    response: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }
    if payload.get("blocked") is True or ("error" in payload and payload.get("error")):
        response["isError"] = True
    return response

def _notification_writer(
    notification_queue: queue.Queue[dict[str, Any] | None],
    stdout_lock: threading.Lock,
) -> None:
    """
    Copy queued MCP notifications to MCP stdout.

    :param notification_queue: Queue populated by the control HTTP
        endpoint.
    :param stdout_lock: Lock protecting JSON-RPC writes to stdout.
    :returns: None after a ``None`` sentinel.
    """
    while True:
        payload = notification_queue.get()
        if payload is None:
            return
        _write_jsonrpc(payload, stdout_lock)

def _stdio_jsonrpc_loop(
    tools: dict[str, Tool],
    stdout_lock: threading.Lock,
    bridge_dir: Path,
) -> None:
    """
    Run the minimal MCP JSON-RPC stdio loop.

    :param tools: Omnigent tools exposed over MCP.
    :param stdout_lock: Lock protecting JSON-RPC writes to stdout.
    :param bridge_dir: Bridge directory path used to read the
        active tool relay.
    :returns: None when stdin reaches EOF.
    """
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None or not isinstance(method, str):
            continue
        # Per-request guard: a failure handling ONE request must never tear
        # down the long-lived MCP server (which would surface to Claude Code
        # as ``-32000: Connection closed`` and drop every tool until respawn).
        # Convert any handler exception into a JSON-RPC error response so the
        # offending call fails cleanly and the stdio loop keeps serving. The
        # individual handlers already return ``_mcp_error`` content for
        # expected failures; this catches the unexpected (e.g. a bug in a
        # tool, or an OSError that slipped a narrower except).
        try:
            result = _handle_mcp_request(method, message.get("params"), tools, bridge_dir)
            response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        except Exception as exc:  # noqa: BLE001 - top-level loop guard keeps the server alive.
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                # -32603 is the JSON-RPC 2.0 "Internal error" code.
                "error": {"code": -32603, "message": f"internal error: {exc}"},
            }
        _write_jsonrpc(response, stdout_lock)

def _handle_mcp_request(
    method: str,
    params: Any,
    tools: dict[str, Tool],
    bridge_dir: Path,
) -> dict[str, Any]:
    """
    Handle one MCP request.

    :param method: JSON-RPC method name, e.g. ``"initialize"``.
    :param params: Request params object.
    :param tools: Omnigent tools exposed over MCP.
    :param bridge_dir: Bridge directory path used to read the
        active tool relay.
    :returns: MCP result object.
    """
    if method == "initialize":
        return {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": True},
            },
            "serverInfo": {
                "name": _MCP_SERVER_NAME,
                "version": "0.1.0",
            },
            "instructions": (
                "Omnigent tools are available as MCP tools when the "
                "active Omnigent turn advertises them; local sys_os_* "
                "tools are available outside an active turn for "
                "workspace file and shell access."
            ),
        }
    if method == "tools/list":
        return {"tools": _combined_mcp_tool_schemas(tools, bridge_dir)}
    if method == "tools/call":
        return _call_mcp_tool(params, tools, bridge_dir)
    if method == "ping":
        return {}
    return {}

def _mcp_tool_schema(tool: Tool) -> dict[str, Any]:
    """
    Convert an Omnigent tool schema into MCP tool-list shape.

    :param tool: Tool instance, e.g. ``SysOsReadTool``.
    :returns: MCP tool descriptor.
    """
    schema = tool.get_schema()["function"]
    return {
        "name": schema["name"],
        "description": schema.get("description", ""),
        "inputSchema": schema.get("parameters", {"type": "object", "properties": {}}),
    }

def _mcp_tool_schema_from_spec(tool_spec: dict[str, Any]) -> dict[str, Any]:
    """
    Convert an Omnigent tool schema dict into MCP tool-list shape.

    :param tool_spec: Tool schema from an active harness turn, e.g.
        ``{"name": "sys_os_shell", "parameters": {...}}``.
    :returns: MCP tool descriptor.
    """
    name = tool_spec.get("name")
    description = tool_spec.get("description")
    parameters = tool_spec.get("parameters")
    return {
        "name": name if isinstance(name, str) else "",
        "description": description if isinstance(description, str) else "",
        "inputSchema": parameters if isinstance(parameters, dict) else _empty_object_schema(),
    }

def _call_mcp_tool(
    params: Any,
    tools: dict[str, Tool],
    bridge_dir: Path,
) -> dict[str, Any]:
    """
    Execute one MCP tool call.

    :param params: MCP tool-call params, e.g.
        ``{"name": "sys_os_read", "arguments": {"path": "README.md"}}``.
    :param tools: Omnigent tools exposed over MCP.
    :param bridge_dir: Bridge directory path used to read the
        active tool relay.
    :returns: MCP tool-call result.
    """
    if not isinstance(params, dict):
        return _mcp_error("tool call params must be an object")
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(name, str):
        return _mcp_error(f"unknown tool: {name!r}")
    if not isinstance(arguments, dict):
        arguments = {}
    if name in _read_relay_tool_names(bridge_dir):
        return _call_relay_tool(bridge_dir, name, arguments)
    if name not in tools:
        return _mcp_error(f"unknown tool: {name!r}")
    bridge_config = _read_json_file(bridge_dir / _CONFIG_FILE)
    workspace_raw = bridge_config.get("workspace") if isinstance(bridge_config, dict) else None
    workspace = Path(workspace_raw) if isinstance(workspace_raw, str) and workspace_raw else None
    ctx = ToolContext(
        task_id="claude-native",
        agent_id="claude-native-ui",
        workspace=workspace,
        conversation_id=read_active_session_id(bridge_dir),
    )
    result = tools[name].invoke(json.dumps(arguments), ctx)
    return {"content": [{"type": "text", "text": result}]}

def _call_relay_tool(
    bridge_dir: Path,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """
    Call the active harness turn's tool relay.

    :param bridge_dir: Bridge directory path used to read
        ``tool_relay.json``.
    :param name: Tool name, e.g. ``"sys_terminal_launch"``.
    :param arguments: Decoded tool arguments.
    :returns: MCP tool-call result.
    """
    relay = _read_json_file(bridge_dir / _TOOL_RELAY_FILE)
    token = relay.get("token")
    url = relay.get("url")
    if not isinstance(token, str) or not isinstance(url, str):
        return _mcp_error("active Omnigent tool relay is missing url/token")
    payload = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
    req = request.Request(
        f"{url}/tool",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=_TOOL_RELAY_POST_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            if resp.status >= 400:
                return _mcp_error(f"tool relay POST failed with HTTP {resp.status}")
    # ``OSError`` (the base of ``error.URLError``) also covers the bare
    # timeout / reset errors that ``urlopen`` raises mid-read —
    # ``TimeoutError``, ``socket.timeout``, ``ConnectionResetError`` — which
    # are NOT ``URLError`` instances. Catching the base class returns a clean
    # MCP error for all of them instead of letting the exception propagate up
    # through ``_call_mcp_tool`` → ``_stdio_jsonrpc_loop`` and kill the MCP
    # server.
    except OSError as exc:
        return _mcp_error(f"failed to call Omnigent tool relay: {exc}")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return _mcp_error("Omnigent tool relay returned malformed JSON")
    if not isinstance(decoded, dict):
        return _mcp_error("Omnigent tool relay returned non-object JSON")
    return decoded

def _mcp_error(message: str) -> dict[str, Any]:
    """
    Build an MCP error-content tool result.

    :param message: Human-readable error message.
    :returns: MCP tool-call result marked as an error.
    """
    return {"content": [{"type": "text", "text": json.dumps({"error": message})}], "isError": True}

def _normalize_relay_tool_specs(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalize active-turn tool schemas before advertising them.

    :param tools: Tool schema dicts from the harness request, e.g.
        ``[{"name": "sys_os_read", "parameters": {...}}]``.
    :returns: Schemas containing only fields the MCP bridge needs.
    """
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = tool.get("description")
        parameters = tool.get("parameters")
        normalized.append(
            {
                "name": name,
                "description": description if isinstance(description, str) else "",
                "parameters": (
                    parameters if isinstance(parameters, dict) else _empty_object_schema()
                ),
            }
        )
    return normalized

def _empty_object_schema() -> dict[str, Any]:
    """
    Return a minimal JSON object schema.

    :returns: ``{"type": "object", "properties": {}}``.
    """
    return {"type": "object", "properties": {}}

def _build_tools(config: dict[str, Any]) -> tuple[dict[str, Tool], Callable[[], None]]:
    """
    Build Omnigent MCP tools served by the bridge.

    :param config: Bridge config JSON object.
    :returns: ``(tools, close_tools)`` where ``close_tools``
        releases any helper processes.
    """
    workspace_raw = config.get("workspace")
    workspace = Path(workspace_raw) if isinstance(workspace_raw, str) and workspace_raw else None
    os_env: OSEnvironment | None = None
    if workspace is not None:
        spec = OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
            fork=False,
        )
        os_env = create_os_environment(spec)
    tools = {tool.name(): tool for tool in build_os_env_tools(os_env)} if os_env else {}

    def _close_tools() -> None:
        """Close helper resources owned by this bridge server."""
        if os_env is not None:
            os_env.close()

    return tools, _close_tools

def _write_jsonrpc(payload: dict[str, Any], stdout_lock: threading.Lock) -> None:
    """
    Write one JSON-RPC message to stdout.

    :param payload: JSON-RPC object to serialize.
    :param stdout_lock: Lock protecting stdout.
    :returns: None.
    """
    raw = json.dumps(payload, separators=(",", ":"))
    with stdout_lock:
        print(raw, flush=True)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _args as _sib_args
    from . import _bridge_io as _sib_bridge_io
    from . import _cost as _sib_cost
    from . import _helpers as _sib_helpers
    from . import _hooks as _sib_hooks
    from . import _inject as _sib_inject
    from . import _tmux as _sib_tmux
    from . import _transcript_convert as _sib_transcript_convert
    from . import _transcript_read as _sib_transcript_read
    from . import _types as _sib_types
    for _key, _value in _sib_args.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_bridge_io.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_cost.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_hooks.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_inject.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_tmux.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript_convert.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_transcript_read.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
