"""Executor that bridges Omnigent turns into a Hermes Agent ACP session.

The local Hermes Agent exposes itself over the **Agent Client Protocol**
(ACP, protocolVersion 1) on stdin/stdout via ``hermes acp`` — the same shape
the upstream ``grok-native`` harness uses for ``grok agent stdio``.

This harness is **self-spawn only**: it spawns its own ``hermes acp`` process,
does a fresh ``session/new``, and handles the conversation self-contained.
There is no leader-socket / TUI-attach mode (that xAI-specific path is dropped).

Model selection is delegated to Hermes — ``HARNESS_HERMES_MODEL`` is optional
and ``None`` means "let Hermes pick its own model" (model-agnostic, per Kade's
charter). Tools run inside Hermes' own agent loop
(``handles_tools_internally`` → ``True``).
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

from bytedesk_omnigent.harnesses.config_apply import apply_spec_to_hermes

_logger = logging.getLogger(__name__)

# ── Env-var knobs (set by hermes_native_harness from the agent spec / runner) ─
_ENV_HERMES_BIN = "HARNESS_HERMES_BIN"
_ENV_MODEL = "HARNESS_HERMES_MODEL"
_ENV_CWD = "HARNESS_HERMES_CWD"


def _default_hermes_bin() -> str:
    """The default ``hermes`` binary — ``HARNESS_HERMES_BIN`` env, else ``hermes``.

    Read at construction time (not import time) so a binary param / env set after
    import is honored, and so importing this module pulls in no process state.
    """
    return os.environ.get(_ENV_HERMES_BIN, "hermes")

# Sentinel pushed onto the per-turn queue when the session/prompt response
# lands, so run_turn drains all preceding session/update notifications first.
_TURN_DONE = object()

# ACP request timeout for handshake calls. Prompt has no timeout — agent turns
# are unbounded.
_HANDSHAKE_TIMEOUT_S = 60.0


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


# ── Shared update translator ───────────────────────────────────────────────


async def _translate_update(
    update: dict[str, Any],
    tool_names: dict[str, str],
) -> AsyncIterator[ExecutorEvent]:
    """Translate one ACP ``session/update`` payload into ExecutorEvents.

    Maps conservatively: any assistant message text → :class:`TextChunk`,
    reasoning/thought text → :class:`ReasoningChunk`, a tool call →
    :class:`ToolCallRequest`, and a completed/failed tool-call update →
    :class:`ToolCallComplete`. Unknown update kinds with a ``content.text``
    fall through as a :class:`TextChunk` passthrough so nothing is dropped.
    """
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
    else:
        # Unknown kind: passthrough any text content so output is never lost.
        text = (update.get("content") or {}).get("text")
        if isinstance(text, str) and text:
            yield TextChunk(text=text)


# ── Low-level ACP stdio session ─────────────────────────────────────────────


class _HermesAcpSession:
    """A persistent ``hermes acp`` subprocess + one ACP session.

    Owns the JSON-RPC-over-stdio request/response/notification plumbing and a
    single self-owned session created via ``session/new``.
    """

    def __init__(
        self, *, cwd: str, model: str | None, hermes_bin: str | None = None
    ) -> None:
        self._cwd = cwd
        self._model = model
        self._hermes_bin = hermes_bin or _default_hermes_bin()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # Per-turn queue: session/update payloads + _TURN_DONE.
        self._turn_q: asyncio.Queue[Any] | None = None
        self._prompt_lock = asyncio.Lock()
        self._session_id: str | None = None

    # -- JSON-RPC plumbing ---------------------------------------------------

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("hermes acp process is not running")
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
                _logger.warning("hermes ACP dispatch error", exc_info=True)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        # Response to one of our requests.
        if method is None and "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        # Agent→client permission request: auto-approve.
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
        # Streamed turn output.
        if method == "session/update":
            params = msg.get("params") or {}
            update = params.get("update") or {}
            queue = self._turn_q
            if queue is not None:
                queue.put_nowait(update)
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
                _logger.info("hermes stderr: %s", text)

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

    # -- Session lifecycle ---------------------------------------------------

    async def start(self) -> None:
        argv = [self._hermes_bin, "acp"]
        await self._start_process(argv, self._cwd)
        await self._request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {}},
            timeout=_HANDSHAKE_TIMEOUT_S,
        )
        new_params: dict[str, Any] = {"cwd": self._cwd, "mcpServers": []}
        if self._model:
            new_params["model"] = self._model
        resp = await self._request(
            "session/new",
            new_params,
            timeout=_HANDSHAKE_TIMEOUT_S,
        )
        self._session_id = (resp.get("result") or {}).get("sessionId")
        if not self._session_id:
            raise RuntimeError(f"hermes session/new returned no sessionId: {resp}")
        _logger.info(
            "hermes ACP session started: session_id=%s cwd=%s",
            self._session_id,
            self._cwd,
        )

    async def prompt(self, text: str) -> AsyncIterator[ExecutorEvent]:
        async with self._prompt_lock:
            if self._proc is None:
                await self.start()
            if self._session_id is None:
                yield ExecutorError(message="hermes session not initialized", retryable=True)
                return
            async for event in self._prompt_session(self._session_id, text):
                yield event

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
                message=f"hermes session/prompt error: {resp['error']}", retryable=True
            )
        else:
            yield TurnComplete(response=None)

    async def cancel(self) -> None:
        if self._proc is None or self._session_id is None:
            return
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": self._session_id},
                }
            )
        except Exception:  # noqa: BLE001
            _logger.debug("hermes session/cancel failed", exc_info=True)

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


# ── Executor ───────────────────────────────────────────────────────────────


class HermesNativeExecutor(Executor):
    """Harness-side executor that runs Omnigent turns on a Hermes ACP session.

    Self-spawn only: owns a single ``hermes acp`` subprocess and ACP session.
    On each turn it reconciles ``SOUL.md`` from the incoming ``system_prompt``
    (idempotent), then delivers the latest user text via ``session/prompt``.
    """

    def __init__(self, model: str | None = None, hermes_bin: str | None = None) -> None:
        self._model = model or (os.environ.get(_ENV_MODEL) or None)
        # Binary is a constructor param (BDP-2349 #42); the env is just the default
        # so existing HARNESS_HERMES_BIN callers are byte-identical.
        self._hermes_bin = hermes_bin or _default_hermes_bin()
        cwd = _resolve_cwd()
        _logger.info(
            "hermes-native: self-spawn mode (model=%s bin=%s)",
            self._model,
            self._hermes_bin,
        )
        self._session = _HermesAcpSession(
            cwd=cwd, model=self._model, hermes_bin=self._hermes_bin
        )

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        """Hermes runs its own tool loop inside the ACP agent."""
        return True

    async def interrupt_session(self, session_key: str) -> bool:
        del session_key
        await self._session.cancel()
        return True

    async def close(self) -> None:
        await self._session.close()

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one Omnigent turn as a Hermes ``session/prompt``.

        :param messages: Conversation history; the latest user message is sent.
        :param tools: Omnigent tool schemas — ignored; Hermes owns its tools.
        :param system_prompt: The agent persona; reconciled into ``SOUL.md``
            (idempotent) at turn start before the prompt.
        :param config: Per-turn config; model is pinned at session start.
        """
        del tools, config
        # Reconcile SOUL.md from the (possibly updated) spec before the turn.
        # Never fail the turn on an apply error.
        try:
            if system_prompt:
                changed = apply_spec_to_hermes(system_prompt)
                if changed:
                    _logger.info("hermes-native: SOUL.md reconciled from spec")
        except Exception:  # noqa: BLE001
            _logger.warning("hermes-native: SOUL.md apply failed", exc_info=True)

        text = _latest_user_text(messages)
        if not text:
            yield ExecutorError(message="hermes-native turn had no user input to send")
            return
        try:
            async for event in self._session.prompt(text):
                yield event
        except Exception as exc:  # noqa: BLE001
            _logger.warning("hermes-native run_turn failed", exc_info=True)
            yield ExecutorError(message=f"hermes-native executor error: {exc}")
