"""Executor that bridges Omnigent turns into a grok agent ACP session.

xAI's Grok Build CLI (``grok``, npm ``@xai-official/grok``) exposes the agent
over the **Agent Client Protocol** (ACP, protocolVersion 1) on stdin/stdout via
``grok agent stdio``.

**Two operating modes:**

TUI mode (``HARNESS_GROK_LEADER_SOCKET`` env var is set)
    The runner launched a bare ``grok`` TUI in the Terminal panel.  That TUI
    auto-started a leader daemon listening on the configured Unix socket.  This
    executor attaches via ``grok agent stdio --leader-socket <path>``, discovers
    the TUI's resident session from the ``_x.ai/sessions/changed`` ACP
    notification, loads it with ``session/load``, and injects turns via
    ``session/prompt`` so they render in the Terminal panel.

Self-spawn mode (no leader socket set)
    The executor spawns its own ``grok agent stdio`` process, does a fresh
    ``session/new``, and handles the conversation self-contained (Chat view
    only).  Used for local testing and as a fallback when the TUI leader is
    not running.

Auth is Grok's cached subscription OAuth token (``~/.grok/auth.json``).
No ``XAI_API_KEY`` / ``OPENAI_API_KEY`` is required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
)

_logger = logging.getLogger(__name__)

# ── Env-var knobs (set by grok_native_harness from the agent spec / runner) ──
_GROK_BIN = os.environ.get("HARNESS_GROK_BIN", "grok")
_ENV_MODEL = "HARNESS_GROK_MODEL"
_ENV_CWD = "HARNESS_GROK_CWD"

# Bridge env vars (set by runner/app.py → build_grok_native_spawn_env).
_ENV_LEADER_SOCKET = "HARNESS_GROK_LEADER_SOCKET"
_ENV_BRIDGE_DIR = "HARNESS_GROK_NATIVE_BRIDGE_DIR"
_ENV_SESSION_ID = "HARNESS_GROK_NATIVE_REQUEST_SESSION_ID"

# Sentinel pushed onto the per-turn queue when the session/prompt response
# lands, so run_turn drains all preceding session/update notifications first.
_TURN_DONE = object()

# ACP request timeout for handshake calls.  Prompt has no timeout — agent
# turns are unbounded.
_HANDSHAKE_TIMEOUT_S = 60.0

# How long to wait for the leader to advertise a resident session via
# ``_x.ai/sessions/changed`` after ``initialize`` before falling back to a
# self-owned ``session/new``.  Kept short because the fallback always works —
# a long wait would only add first-turn latency when the TUI is idle (which is
# the common case for web-driven sessions: the TUI holds no resident session
# until someone types in it).
_SESSION_DISCOVER_TIMEOUT_S = 6.0

# After typing the first message into the TUI (bootstrap), how long to wait for
# the leader to advertise the resulting resident session via
# ``_x.ai/sessions/changed`` before giving up and using a self-owned session.
_TUI_BOOTSTRAP_SID_TIMEOUT_S = 30.0

# Reading a TUI-driven turn (one the TUI runs because we typed into it): how long
# to wait for the next streamed token before re-checking whether the turn ended
# (the session's ``activity`` went back to ``idle``), and an overall ceiling.
_TUI_TURN_TOKEN_TIMEOUT_S = 1.0
_TUI_TURN_MAX_S = 600.0


def _resolve_cwd() -> str:
    return os.environ.get(_ENV_CWD) or os.environ.get("HOME") or os.getcwd()


def _latest_user_text(messages: list[Message]) -> str:
    """Extract the latest user message as plain text for the ACP prompt."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"input_text", "text"}:
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
            return "\n".join(parts)
        if content is not None:
            return json.dumps(content, ensure_ascii=True)
        return ""
    return ""


# ── Shared ACP I/O mixin ───────────────────────────────────────────────────


class _GrokAcpBase:
    """Low-level ACP stdio request/response/notification handling."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # Per-turn queue: session/update payloads + _TURN_DONE.
        self._turn_q: asyncio.Queue[Any] | None = None
        # Notification listeners: method → list of one-shot futures.
        self._notification_listeners: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self._prompt_lock = asyncio.Lock()
        # Sessions advertised by the leader via ``_x.ai/sessions/changed``,
        # accumulated as they arrive (keyed by session id).  Buffered here in
        # the reader loop so discovery never races a notification that lands
        # during/just after ``initialize``.
        self._advertised_sessions: dict[str, dict[str, Any]] = {}
        # Fired on every ``_x.ai/sessions/changed`` so waiters can react to a
        # new session / an activity transition without polling tightly.
        self._sessions_changed_event = asyncio.Event()
        # Per-session update queues for reading a TUI-driven turn (a turn the
        # TUI runs because we typed into it) when no ``_prompt_session`` owns
        # ``_turn_q``.  Keyed by grok session id.
        self._session_update_qs: dict[str, asyncio.Queue[dict[str, Any]]] = {}

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("grok agent stdio process is not running")
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None,
    ) -> dict[str, Any]:
        rid = self._next_request_id()
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        finally:
            self._pending.pop(rid, None)

    async def _reader_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            try:
                await self._dispatch(msg)
            except Exception:  # noqa: BLE001
                _logger.warning("grok ACP dispatch error", exc_info=True)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        # Response to one of our requests.
        if method is None and "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        # Notification: fire one-shot listeners.
        if method:
            listeners = self._notification_listeners.pop(method, [])
            for fut in listeners:
                if not fut.done():
                    fut.set_result(msg)
        # Buffer the leader's resident-session roster.  The payload carries
        # ``upserted``/``removed`` arrays (NOT a ``sessions`` array), each entry
        # an object with ``sessionId`` + ``resident``.
        if method == "_x.ai/sessions/changed":
            params = msg.get("params") or {}
            for entry in params.get("upserted") or []:
                sid = entry.get("sessionId") or entry.get("id")
                if sid:
                    self._advertised_sessions[sid] = entry
            for entry in params.get("removed") or []:
                sid = (
                    entry.get("sessionId") or entry.get("id")
                    if isinstance(entry, dict)
                    else entry
                )
                self._advertised_sessions.pop(sid, None)
            self._sessions_changed_event.set()
            return
        # Agent→client permission request.
        if method == "session/request_permission":
            await self._auto_approve(msg)
            return
        if method in {"fs/read_text_file", "fs/write_text_file"}:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32601, "message": "fs not supported by this client"},
                }
            )
            return
        # Streamed turn output.  ``session/update`` is what our own
        # ``session/prompt`` turns stream; ``_x.ai/session_notification`` is the
        # same shape the leader broadcasts for a turn the TUI runs itself.
        if method in {"session/update", "_x.ai/session_notification"}:
            params = msg.get("params") or {}
            update = params.get("update") or {}
            queue = self._turn_q
            if queue is not None:
                # A ``_prompt_session`` turn is in flight — feed its reader.
                queue.put_nowait(update)
            else:
                # No active prompt: this is a TUI-driven turn.  Buffer it under
                # its session id so a bootstrap reader can drain it in order
                # (including updates that arrived before it started reading).
                sid = params.get("sessionId")
                if sid:
                    self._session_update_qs.setdefault(sid, asyncio.Queue()).put_nowait(update)
            return

    async def _auto_approve(self, msg: dict[str, Any]) -> None:
        options = (msg.get("params") or {}).get("options", [])
        allow = next(
            (
                o
                for o in options
                if "allow" in (str(o.get("kind", "")) + str(o.get("optionId", ""))).lower()
            ),
            options[0] if options else None,
        )
        option_id = (allow or {}).get("optionId")
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
            }
        )

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                _logger.info("grok stderr: %s", text)

    async def _wait_for_notification(self, method: str, timeout: float) -> dict[str, Any]:
        """Wait for a single ACP notification of *method* from the agent."""
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._notification_listeners.setdefault(method, []).append(fut)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            listeners = self._notification_listeners.get(method, [])
            try:
                listeners.remove(fut)
            except ValueError:
                pass
            raise

    def _pick_resident_session(self) -> str | None:
        """Return a resident session id from the buffered leader roster, if any."""
        for sid, meta in self._advertised_sessions.items():
            if meta.get("resident"):
                return sid
        # No explicitly-resident session — accept any advertised one.
        return next(iter(self._advertised_sessions), None)

    async def _discover_resident_session(self, timeout: float) -> str | None:
        """Poll the buffered ``_x.ai/sessions/changed`` roster for a session."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            sid = self._pick_resident_session()
            if sid is not None:
                return sid
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.2)

    async def _start_process(self, argv: list[str], cwd: str) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def _prompt_session(
        self,
        session_id: str,
        text: str,
    ) -> AsyncIterator[ExecutorEvent]:
        """Send a ``session/prompt`` and yield translated events."""
        self._turn_q = asyncio.Queue()
        rid = self._next_request_id()
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            }
        )

        async def _signal_done() -> None:
            try:
                await fut
            finally:
                if self._turn_q is not None:
                    self._turn_q.put_nowait(_TURN_DONE)

        done_task = asyncio.create_task(_signal_done())
        tool_names: dict[str, str] = {}
        try:
            while True:
                item = await self._turn_q.get()
                if item is _TURN_DONE:
                    break
                async for event in _translate_update(item, tool_names):
                    yield event
        finally:
            self._pending.pop(rid, None)
            self._turn_q = None
            if not done_task.done():
                done_task.cancel()

        resp = fut.result() if fut.done() else {}
        if isinstance(resp, dict) and "error" in resp:
            yield ExecutorError(
                message=f"grok session/prompt error: {resp['error']}", retryable=True
            )
        else:
            yield TurnComplete(response=None)

    async def cancel(self, session_id: str) -> None:
        if self._proc is None:
            return
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                }
            )
        except Exception:  # noqa: BLE001
            _logger.debug("grok session/cancel failed", exc_info=True)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        self._proc = None


# ── TUI leader-attach session ──────────────────────────────────────────────


class _GrokTuiAcpSession(_GrokAcpBase):
    """
    A ``grok agent stdio --leader-socket`` connection that mirrors turns into
    the running Grok TUI panel.

    The bare ``grok`` TUI only renders a session it *owns* — a session created
    by another client (our ``session/new``) streams replies fine but never
    appears in the panel.  So the bridge works in two modes:

    * **Bootstrap (first turn / after a restart, when we hold no session id):**
      type the user's message into the TUI via ``tmux send-keys`` (the runner
      advertised the pane in the bridge dir).  The TUI then owns + renders the
      turn; we learn its session id from ``_x.ai/sessions/changed`` and read the
      streamed reply off the leader.
    * **Steady state (we already hold the TUI's session id):** deliver the turn
      over ACP ``session/prompt`` to that resident session — which the TUI also
      renders — and stream the reply through the normal prompt path.

    If no tmux pane was advertised (no panel to mirror into), we fall back to a
    self-owned ``session/new`` so the chat still works headlessly.
    """

    def __init__(self, *, leader_socket: str, cwd: str, bridge_dir: Path | None) -> None:
        super().__init__()
        self._leader_socket = leader_socket
        self._cwd = cwd
        self._bridge_dir = bridge_dir
        self._grok_session_id: str | None = None
        self._loaded = False
        self._initialized = False

    async def start(self) -> None:
        # ``grok agent stdio --leader-socket <path>`` connects to the TUI's
        # per-conversation leader instead of spawning a standalone agent.
        argv = [_GROK_BIN, "agent", "stdio", "--leader-socket", self._leader_socket]
        await self._start_process(argv, self._cwd)
        await self._request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {}},
            timeout=_HANDSHAKE_TIMEOUT_S,
        )
        self._initialized = True

    async def prompt(self, text: str) -> AsyncIterator[ExecutorEvent]:
        async with self._prompt_lock:
            if not self._initialized:
                await self.start()
            # Steady state: we already own the TUI's resident session id —
            # deliver over ACP session/prompt (renders in the panel + streams).
            if self._grok_session_id is not None:
                await self._ensure_loaded(self._grok_session_id)
                async for event in self._prompt_session(self._grok_session_id, text):
                    yield event
                return
            # First turn / post-restart: bootstrap the TUI's resident session by
            # typing into the pane, then read the turn the TUI runs.
            async for event in self._bootstrap_turn(text):
                yield event

    async def _ensure_loaded(self, sid: str) -> None:
        """``session/load`` the resident session once (idempotent, best-effort).

        The leader returns an error for an already-active resident session, so
        the response is not a validity signal — we only need the load so this
        connection is subscribed to the session's ``session/update`` stream.
        """
        if self._loaded:
            return
        try:
            await self._request(
                "session/load", {"sessionId": sid}, timeout=_HANDSHAKE_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001
            _logger.debug("grok TUI bridge: session/load(%s) returned error", sid, exc_info=True)
        self._loaded = True

    async def _bootstrap_turn(self, text: str) -> AsyncIterator[ExecutorEvent]:
        """Type the first message into the TUI and stream the turn it runs."""
        injected = await self._inject_into_tui(text)
        if not injected:
            # No tmux pane advertised — nothing to mirror into. Fall back to a
            # self-owned session so the chat still works.
            _logger.info("grok TUI bridge: no pane advertised; using self-owned session")
            resp = await self._request(
                "session/new", {"cwd": self._cwd, "mcpServers": []}, timeout=_HANDSHAKE_TIMEOUT_S
            )
            sid = (resp.get("result") or {}).get("sessionId")
            if not sid:
                yield ExecutorError(message=f"grok session/new returned no sessionId: {resp}")
                return
            self._grok_session_id = sid
            self._loaded = True
            self._persist_state(sid)
            async for event in self._prompt_session(sid, text):
                yield event
            return

        # The TUI is (or will shortly be) running the turn. Discover its session
        # id from the leader's roster, subscribe, and stream the reply.
        sid = await self._discover_resident_session(_TUI_BOOTSTRAP_SID_TIMEOUT_S)
        if sid is None:
            yield ExecutorError(
                message="grok TUI bridge: TUI did not advertise a session after injecting "
                "the first message",
                retryable=True,
            )
            return
        self._grok_session_id = sid
        self._persist_state(sid)
        await self._ensure_loaded(sid)
        async for event in self._read_tui_turn(sid):
            yield event

    async def _inject_into_tui(self, text: str) -> bool:
        """Type *text* into the TUI pane via the bridge's tmux helper."""
        if self._bridge_dir is None:
            return False
        try:
            from omnigent.grok_native_bridge import inject_user_message

            # Blocking subprocess/tmux work — keep the event loop responsive.
            return await asyncio.to_thread(
                inject_user_message, self._bridge_dir, content=text
            )
        except Exception:  # noqa: BLE001
            _logger.warning("grok TUI bridge: inject into pane failed", exc_info=True)
            return False

    async def _read_tui_turn(self, sid: str) -> AsyncIterator[ExecutorEvent]:
        """Stream a turn the TUI is running, read from the per-session queue."""
        queue = self._session_update_qs.setdefault(sid, asyncio.Queue())
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _TUI_TURN_MAX_S
        tool_names: dict[str, str] = {}
        saw_activity = False  # seen the turn actually start (working) or emit output
        while loop.time() < deadline:
            try:
                update = await asyncio.wait_for(queue.get(), _TUI_TURN_TOKEN_TIMEOUT_S)
            except asyncio.TimeoutError:
                activity = (self._advertised_sessions.get(sid) or {}).get("activity")
                if activity == "working":
                    saw_activity = True
                # End once the turn has run and gone idle with nothing buffered.
                if saw_activity and activity == "idle" and queue.empty():
                    break
                continue
            saw_activity = True
            async for event in _translate_update(update, tool_names):
                yield event
        yield TurnComplete(response=None)

    def _persist_state(self, sid: str) -> None:
        """Persist the resolved session id for diagnostics / reconnection."""
        if self._bridge_dir is None:
            return
        try:
            from omnigent.grok_native_bridge import GrokNativeBridgeState, write_bridge_state

            write_bridge_state(
                self._bridge_dir,
                GrokNativeBridgeState(
                    session_id=os.environ.get(_ENV_SESSION_ID, ""),
                    grok_session_id=sid,
                    leader_socket=self._leader_socket,
                ),
            )
        except Exception:  # noqa: BLE001
            _logger.debug("grok TUI bridge: state write failed", exc_info=True)

    async def do_cancel(self) -> None:
        if self._grok_session_id:
            await self.cancel(self._grok_session_id)


# ── Self-spawn session ─────────────────────────────────────────────────────


class _GrokAcpSession(_GrokAcpBase):
    """
    A persistent ``grok agent stdio`` subprocess + one ACP session.

    Self-spawn mode: creates its own standalone grok process and session.
    Used when no TUI leader socket is configured (Chat view only).
    """

    def __init__(self, *, cwd: str, model: str | None) -> None:
        super().__init__()
        self._cwd = cwd
        self._model = model
        self._session_id: str | None = None

    async def start(self) -> None:
        # ``grok agent stdio`` accepts ONLY --debug/--debug-file/--leader-socket.
        # --always-approve/--no-auto-update/--model live on the parent ``grok
        # agent`` (or the top-level TUI), NOT the stdio subcommand.
        argv = [_GROK_BIN, "agent", "stdio"]
        await self._start_process(argv, self._cwd)

        await self._request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {}},
            timeout=_HANDSHAKE_TIMEOUT_S,
        )
        resp = await self._request(
            "session/new",
            {"cwd": self._cwd, "mcpServers": []},
            timeout=_HANDSHAKE_TIMEOUT_S,
        )
        self._session_id = (resp.get("result") or {}).get("sessionId")
        if not self._session_id:
            raise RuntimeError(f"grok session/new returned no sessionId: {resp}")
        _logger.info(
            "grok ACP session started: session_id=%s cwd=%s",
            self._session_id,
            self._cwd,
        )

    async def prompt(self, text: str) -> AsyncIterator[ExecutorEvent]:
        async with self._prompt_lock:
            if self._proc is None:
                await self.start()
            if self._session_id is None:
                yield ExecutorError(message="grok session not initialized", retryable=True)
                return
            async for event in self._prompt_session(self._session_id, text):
                yield event

    async def do_cancel(self) -> None:
        if self._session_id:
            await self.cancel(self._session_id)


# ── Shared update translator ───────────────────────────────────────────────


async def _translate_update(
    update: dict[str, Any],
    tool_names: dict[str, str],
) -> AsyncIterator[ExecutorEvent]:
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        text = (update.get("content") or {}).get("text")
        if isinstance(text, str) and text:
            yield TextChunk(text=text)
    elif kind == "agent_thought_chunk":
        text = (update.get("content") or {}).get("text")
        if isinstance(text, str) and text:
            yield ReasoningChunk(delta=text, event_type="reasoning_text")
    elif kind == "tool_call":
        call_id = str(update.get("toolCallId") or "")
        name = update.get("title") or update.get("kind") or "tool"
        tool_names[call_id] = name
        yield ToolCallRequest(name=name, args={}, metadata={"call_id": call_id})
    elif kind == "tool_call_update":
        status = update.get("status")
        if status in {"completed", "failed"}:
            call_id = str(update.get("toolCallId") or "")
            yield ToolCallComplete(
                name=tool_names.get(call_id, "tool"),
                status=ToolCallStatus.SUCCESS if status == "completed" else ToolCallStatus.ERROR,
                metadata={"call_id": call_id},
            )
    # user_message_chunk / available_commands_update / current_mode_update: ignore.


# ── Executor ───────────────────────────────────────────────────────────────


class GrokNativeExecutor(Executor):
    """
    Harness-side executor that runs Omnigent turns on a grok ACP session.

    Picks **TUI mode** when ``HARNESS_GROK_LEADER_SOCKET`` is set (the runner
    launched a Grok TUI terminal), otherwise falls back to **self-spawn mode**
    (Chat view only).
    """

    def __init__(self, model: str | None = None) -> None:
        self._model = model or (os.environ.get(_ENV_MODEL) or None)
        leader_socket = os.environ.get(_ENV_LEADER_SOCKET, "").strip()
        cwd = _resolve_cwd()

        if leader_socket:
            bridge_dir_str = os.environ.get(_ENV_BRIDGE_DIR, "").strip()
            bridge_dir = Path(bridge_dir_str) if bridge_dir_str else None
            _logger.info("grok-native: TUI mode, leader=%s", leader_socket)
            self._tui_session: _GrokTuiAcpSession | None = _GrokTuiAcpSession(
                leader_socket=leader_socket,
                cwd=cwd,
                bridge_dir=bridge_dir,
            )
            self._session: _GrokAcpSession | None = None
        else:
            _logger.info("grok-native: self-spawn mode")
            self._tui_session = None
            self._session = _GrokAcpSession(cwd=cwd, model=self._model)

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        """grok runs its own coding tools inside the ACP agent."""
        return True

    async def interrupt_session(self, session_key: str) -> bool:
        del session_key
        if self._tui_session is not None:
            await self._tui_session.do_cancel()
        elif self._session is not None:
            await self._session.do_cancel()
        return True

    async def close(self) -> None:
        if self._tui_session is not None:
            await self._tui_session.close()
        if self._session is not None:
            await self._session.close()

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Run one Omnigent turn as a grok ``session/prompt``.

        :param messages: Conversation history; the latest user message is sent.
        :param tools: Omnigent tool schemas — ignored; grok owns its tool surface.
        :param system_prompt: Ignored; the grok session carries its own system prompt.
        :param config: Per-turn config; model is pinned at session start.
        """
        del tools, system_prompt, config
        text = _latest_user_text(messages)
        if not text:
            yield ExecutorError(message="grok-native turn had no user input to send")
            return
        try:
            active = self._tui_session or self._session
            assert active is not None
            async for event in active.prompt(text):
                yield event
        except Exception as exc:  # noqa: BLE001
            _logger.warning("grok-native run_turn failed", exc_info=True)
            yield ExecutorError(message=f"grok-native executor error: {exc}")
