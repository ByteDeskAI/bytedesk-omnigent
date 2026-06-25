"""Seam tests for GrokNativeExecutor and Grok ACP session lifecycle edges."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnigent.inner import grok_native_executor as gne
from omnigent.inner.executor import (
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)
from omnigent.inner.grok_native_executor import (
    GrokNativeExecutor,
    _GrokAcpSession,
    _GrokTuiAcpSession,
    _latest_user_text,
    _resolve_cwd,
    _translate_update,
)


def test_resolve_cwd_prefers_env_then_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARNESS_GROK_CWD", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    assert _resolve_cwd() == str(tmp_path)

    monkeypatch.setenv("HOME", "/home/grok")
    assert _resolve_cwd() == "/home/grok"
    monkeypatch.setenv("HARNESS_GROK_CWD", "/work")
    assert _resolve_cwd() == "/work"


def test_latest_user_text_serializes_non_text_content() -> None:
    messages = [{"role": "user", "content": {"tool": "ping"}}]
    assert _latest_user_text(messages) == '{"tool": "ping"}'


def test_latest_user_text_joins_text_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "line1"},
                {"type": "text", "text": "line2"},
            ],
        }
    ]
    assert _latest_user_text(messages) == "line1\nline2"


def test_latest_user_text_skips_non_user_roles() -> None:
    messages = [
        {"role": "assistant", "content": "ignored"},
        {"role": "user", "content": "send me"},
    ]
    assert _latest_user_text(messages) == "send me"


def test_latest_user_text_returns_empty_when_only_non_user_roles() -> None:
    assert _latest_user_text([{"role": "assistant", "content": "only assistant"}]) == ""


@pytest.mark.asyncio
async def test_send_raises_when_process_missing() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    with pytest.raises(RuntimeError, match="not running"):
        await session._send({"jsonrpc": "2.0"})


@pytest.mark.asyncio
async def test_request_round_trip_and_timeout(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        rid = obj["id"]
        session._pending[rid].set_result({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

    monkeypatch.setattr(session, "_send", _fake_send)
    resp = await session._request("ping", {}, timeout=1.0)
    assert resp["result"]["ok"] is True

    timeout_session = _GrokAcpSession(cwd="/tmp", model=None)

    async def _never_respond(_obj: dict) -> None:
        return None

    monkeypatch.setattr(timeout_session, "_send", _never_respond)
    with pytest.raises(asyncio.TimeoutError):
        await timeout_session._request("slow", {}, timeout=0.01)


@pytest.mark.asyncio
async def test_dispatch_handles_response_permission_and_fs() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    fut = asyncio.get_event_loop().create_future()
    session._pending[7] = fut
    await session._dispatch({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
    assert fut.result()["result"]["ok"] is True

    sent: list[dict] = []

    async def _capture_send(obj: dict) -> None:
        sent.append(obj)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session, "_send", _capture_send)
    await session._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/request_permission",
            "params": {"options": [{"kind": "allow-once", "optionId": "allow"}]},
        }
    )
    assert sent[-1]["result"]["outcome"]["optionId"] == "allow"

    await session._dispatch(
        {"jsonrpc": "2.0", "id": 10, "method": "fs/read_text_file", "params": {}}
    )
    assert sent[-1]["error"]["code"] == -32601

    session._turn_q = asyncio.Queue()
    await session._dispatch(
        {
            "method": "session/update",
            "params": {
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "x"}}
            },
        }
    )
    assert (await session._turn_q.get())["content"]["text"] == "x"
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_dispatch_buffers_sessions_changed_and_tui_notifications() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    await session._dispatch(
        {
            "method": "_x.ai/sessions/changed",
            "params": {
                "upserted": [{"sessionId": "sess-1", "resident": True, "activity": "working"}],
                "removed": [{"sessionId": "sess-old"}],
            },
        }
    )
    assert session._advertised_sessions["sess-1"]["resident"] is True
    assert "sess-old" not in session._advertised_sessions

    await session._dispatch(
        {
            "method": "_x.ai/session_notification",
            "params": {
                "sessionId": "sess-2",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "tui"}},
            },
        }
    )
    queue = session._session_update_qs["sess-2"]
    assert (await queue.get())["content"]["text"] == "tui"


@pytest.mark.asyncio
async def test_reader_and_stderr_loops_ignore_bad_lines(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    class _Stream:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = list(lines)

        async def readline(self) -> bytes:
            return self._lines.pop(0) if self._lines else b""

    session._proc = type(
        "P", (), {"stdout": _Stream([b"\n", b"{bad json\n", b'{"id":1,"result":{}}\n', b""])}
    )()

    dispatched: list[dict] = []

    async def _capture(msg: dict) -> None:
        dispatched.append(msg)

    monkeypatch.setattr(session, "_dispatch", _capture)
    await session._reader_loop()
    assert dispatched == [{"id": 1, "result": {}}]

    session._proc = type("P", (), {"stderr": _Stream([b"log line\n", b""])})()
    await session._stderr_loop()


@pytest.mark.asyncio
async def test_wait_for_notification_times_out(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    with pytest.raises(asyncio.TimeoutError):
        await session._wait_for_notification("session/update", timeout=0.01)


def test_pick_resident_session_prefers_resident_flag() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    session._advertised_sessions = {
        "a": {"resident": False},
        "b": {"resident": True},
    }
    assert session._pick_resident_session() == "b"


@pytest.mark.asyncio
async def test_discover_resident_session_returns_when_buffered() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    session._advertised_sessions["sess-x"] = {"resident": True}
    sid = await session._discover_resident_session(timeout=0.01)
    assert sid == "sess-x"


@pytest.mark.asyncio
async def test_start_raises_when_session_new_missing_id(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    async def _fake_start_process(_argv: list[str], _cwd: str) -> None:
        session._proc = type(
            "P",
            (),
            {"stdin": type("S", (), {"write": lambda *_a, **_k: None, "drain": _async_noop})()},
        )()

    async def _fake_request(method: str, _params: dict, *, timeout: float | None) -> dict:
        if method == "initialize":
            return {"result": {}}
        return {"result": {}}

    monkeypatch.setattr(session, "_start_process", _fake_start_process)
    monkeypatch.setattr(session, "_request", _fake_request)
    with pytest.raises(RuntimeError, match="no sessionId"):
        await session.start()


@pytest.mark.asyncio
async def test_prompt_starts_session_and_surfaces_init_error(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    async def _fake_start() -> None:
        session._session_id = "sess-1"

    async def _fake_prompt_session(_sid: str, _text: str):
        yield TextChunk(text="ok")
        yield TurnComplete(response=None)

    monkeypatch.setattr(session, "start", _fake_start)
    monkeypatch.setattr(session, "_prompt_session", _fake_prompt_session)
    events = [event async for event in session.prompt("hi")]
    assert isinstance(events[0], TextChunk)

    session._session_id = None
    session._proc = object()
    events = [event async for event in session.prompt("hi")]
    assert isinstance(events[0], ExecutorError)


@pytest.mark.asyncio
async def test_prompt_session_yields_error_on_rpc_failure(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        rid = obj["id"]
        fut = session._pending[rid]
        assert session._turn_q is not None
        session._turn_q.put_nowait(gne._TURN_DONE)
        fut.set_result({"error": {"code": -1, "message": "boom"}})

    monkeypatch.setattr(session, "_send", _fake_send)
    events = [event async for event in session._prompt_session("sess-1", "hi")]
    assert any(isinstance(e, ExecutorError) for e in events)


@pytest.mark.asyncio
async def test_cancel_and_close_are_idempotent() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    await session.cancel("sess-1")
    await session.close()

    class _Proc:
        returncode = None

        def terminate(self) -> None:
            self.returncode = 0

    session._proc = _Proc()
    session._session_id = "sess-1"
    session._reader_task = asyncio.create_task(asyncio.sleep(60))
    session._stderr_task = asyncio.create_task(asyncio.sleep(60))
    await session.cancel("sess-1")
    await session.close()
    assert session._proc is None


@pytest.mark.asyncio
async def test_tui_session_steady_state_prompt(monkeypatch) -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)
    session._initialized = True
    session._grok_session_id = "grok-sess"

    async def _fake_ensure_loaded(_sid: str) -> None:
        return None

    async def _fake_prompt_session(_sid: str, _text: str):
        yield TextChunk(text="steady")
        yield TurnComplete(response=None)

    monkeypatch.setattr(session, "_ensure_loaded", _fake_ensure_loaded)
    monkeypatch.setattr(session, "_prompt_session", _fake_prompt_session)
    events = [event async for event in session.prompt("hello")]
    assert events[0].text == "steady"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_tui_bootstrap_falls_back_to_self_owned_session(monkeypatch, tmp_path: Path) -> None:
    session = _GrokTuiAcpSession(
        leader_socket="/tmp/leader.sock",
        cwd="/tmp",
        bridge_dir=tmp_path,
    )
    session._initialized = True

    async def _fake_inject(_text: str) -> bool:
        return False

    async def _fake_request(method: str, _params: dict, *, timeout: float | None) -> dict:
        if method == "session/new":
            return {"result": {"sessionId": "self-sess"}}
        return {"result": {}}

    async def _fake_prompt_session(_sid: str, _text: str):
        yield TextChunk(text="fallback")
        yield TurnComplete(response=None)

    monkeypatch.setattr(session, "_inject_into_tui", _fake_inject)
    monkeypatch.setattr(session, "_request", _fake_request)
    monkeypatch.setattr(session, "_prompt_session", _fake_prompt_session)
    events = [event async for event in session.prompt("bootstrap")]
    assert session._grok_session_id == "self-sess"
    assert events[0].text == "fallback"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_tui_bootstrap_inject_and_read_turn(monkeypatch, tmp_path: Path) -> None:
    session = _GrokTuiAcpSession(
        leader_socket="/tmp/leader.sock",
        cwd="/tmp",
        bridge_dir=tmp_path,
    )
    session._initialized = True

    async def _fake_inject(_text: str) -> bool:
        return True

    async def _fake_discover(_timeout: float) -> str:
        return "tui-sess"

    async def _fake_read_turn(_sid: str):
        yield TextChunk(text="from-tui")
        yield TurnComplete(response=None)

    async def _fake_ensure_loaded(_sid: str) -> None:
        return None

    monkeypatch.setattr(session, "_inject_into_tui", _fake_inject)
    monkeypatch.setattr(session, "_discover_resident_session", _fake_discover)
    monkeypatch.setattr(session, "_read_tui_turn", _fake_read_turn)
    monkeypatch.setattr(session, "_ensure_loaded", _fake_ensure_loaded)
    events = [event async for event in session.prompt("first")]
    assert session._grok_session_id == "tui-sess"
    assert events[0].text == "from-tui"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_tui_bootstrap_errors_when_session_not_discovered(
    monkeypatch, tmp_path: Path
) -> None:
    session = _GrokTuiAcpSession(
        leader_socket="/tmp/leader.sock",
        cwd="/tmp",
        bridge_dir=tmp_path,
    )
    session._initialized = True

    async def _fake_inject(_text: str) -> bool:
        return True

    async def _fake_discover(_timeout: float) -> None:
        return None

    monkeypatch.setattr(session, "_inject_into_tui", _fake_inject)
    monkeypatch.setattr(session, "_discover_resident_session", _fake_discover)
    events = [event async for event in session.prompt("first")]
    assert isinstance(events[0], ExecutorError)


@pytest.mark.asyncio
async def test_tui_inject_returns_false_without_bridge_dir() -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)
    assert await session._inject_into_tui("hi") is False


@pytest.mark.asyncio
async def test_tui_inject_uses_bridge_helper(monkeypatch, tmp_path: Path) -> None:
    session = _GrokTuiAcpSession(
        leader_socket="/tmp/leader.sock",
        cwd="/tmp",
        bridge_dir=tmp_path,
    )
    monkeypatch.setattr(
        "omnigent.grok_native_bridge.inject_user_message",
        lambda _dir, content: True,
    )
    assert await session._inject_into_tui("typed") is True


@pytest.mark.asyncio
async def test_read_tui_turn_drains_on_idle(monkeypatch) -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)
    session._advertised_sessions["sid-1"] = {"activity": "idle"}
    queue = session._session_update_qs.setdefault("sid-1", asyncio.Queue())
    queue.put_nowait({"sessionUpdate": "agent_message_chunk", "content": {"text": "done"}})

    monkeypatch.setattr(
        "omnigent.inner.grok_native_executor._TUI_TURN_TOKEN_TIMEOUT_S",
        0.01,
    )
    monkeypatch.setattr(
        "omnigent.inner.grok_native_executor._TUI_TURN_MAX_S",
        0.05,
    )
    events = [event async for event in session._read_tui_turn("sid-1")]
    assert isinstance(events[0], TextChunk)
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_grok_executor_capabilities_and_interrupt(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_GROK_LEADER_SOCKET", raising=False)
    ex = GrokNativeExecutor(model="grok-2")
    assert ex.supports_streaming() is True
    assert ex.supports_tool_calling() is True
    assert ex.handles_tools_internally() is True
    assert await ex.interrupt_session("ignored") is True
    await ex.close()


@pytest.mark.asyncio
async def test_grok_executor_tui_mode_from_env(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_GROK_LEADER_SOCKET", "/tmp/leader.sock")
    monkeypatch.setenv("HARNESS_GROK_NATIVE_BRIDGE_DIR", "/tmp/bridge")
    ex = GrokNativeExecutor()
    assert ex._tui_session is not None
    assert ex._session is None
    await ex.close()


@pytest.mark.asyncio
async def test_run_turn_no_user_input() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("HARNESS_GROK_LEADER_SOCKET", raising=False)
    ex = GrokNativeExecutor()
    events = [event async for event in ex.run_turn([], [], "persona")]
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_run_turn_streams_self_spawn_prompt(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_GROK_LEADER_SOCKET", raising=False)
    ex = GrokNativeExecutor()

    async def _fake_prompt(text: str):
        assert text == "hello"
        yield TextChunk(text="reply")
        yield TurnComplete(response=None)

    assert ex._session is not None
    monkeypatch.setattr(ex._session, "prompt", _fake_prompt)
    events = [
        event
        async for event in ex.run_turn(
            [{"role": "user", "content": "hello"}],
            [],
            "persona",
        )
    ]
    assert events[0].text == "reply"  # type: ignore[union-attr]
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_run_turn_surfaces_prompt_errors(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_GROK_LEADER_SOCKET", raising=False)
    ex = GrokNativeExecutor()

    async def _raise_prompt(_text: str):
        raise RuntimeError("pipe broken")
        yield  # pragma: no cover

    assert ex._session is not None
    monkeypatch.setattr(ex._session, "prompt", _raise_prompt)
    events = [
        event
        async for event in ex.run_turn(
            [{"role": "user", "content": "ping"}],
            [],
            "persona",
        )
    ]
    assert isinstance(events[0], ExecutorError)
    assert "pipe broken" in events[0].message


@pytest.mark.asyncio
async def test_translate_update_all_kinds() -> None:
    events = [
        event
        async for event in _translate_update(
            {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
            {},
        )
    ]
    assert isinstance(events[0], TextChunk)

    events = [
        event
        async for event in _translate_update(
            {"sessionUpdate": "agent_thought_chunk", "content": {"text": "think"}},
            {},
        )
    ]
    assert isinstance(events[0], ReasoningChunk)

    tool_names: dict[str, str] = {}
    events = [
        event
        async for event in _translate_update(
            {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "grep"},
            tool_names,
        )
    ]
    assert isinstance(events[0], ToolCallRequest)

    events = [
        event
        async for event in _translate_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"},
            tool_names,
        )
    ]
    assert isinstance(events[0], ToolCallComplete)
    assert events[0].status == ToolCallStatus.SUCCESS  # type: ignore[union-attr]

    events = [
        event
        async for event in _translate_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "failed"},
            tool_names,
        )
    ]
    assert events[0].status == ToolCallStatus.ERROR  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_persist_state_writes_bridge_file(monkeypatch, tmp_path: Path) -> None:
    session = _GrokTuiAcpSession(
        leader_socket="/tmp/leader.sock",
        cwd="/tmp",
        bridge_dir=tmp_path,
    )
    monkeypatch.setenv("HARNESS_GROK_NATIVE_REQUEST_SESSION_ID", "conv-1")
    session._persist_state("grok-sid")
    state_file = tmp_path / "state.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["grok_session_id"] == "grok-sid"


@pytest.mark.asyncio
async def test_request_without_timeout(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        rid = obj["id"]
        session._pending[rid].set_result({"jsonrpc": "2.0", "id": rid, "result": {}})

    monkeypatch.setattr(session, "_send", _fake_send)
    resp = await session._request("ping", {}, timeout=None)
    assert resp["result"] == {}


@pytest.mark.asyncio
async def test_send_writes_to_process_stdin(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    writes: list[bytes] = []

    class _Stdin:
        def write(self, data: bytes) -> None:
            writes.append(data)

        async def drain(self) -> None:
            return None

    session._proc = type("P", (), {"stdin": _Stdin()})()
    await session._send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert writes and b'"ping"' in writes[0]


@pytest.mark.asyncio
async def test_reader_loop_logs_dispatch_errors(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    class _Stream:
        def __init__(self) -> None:
            self._lines = [b'{"id": 1, "result": {}}\n', b""]

        async def readline(self) -> bytes:
            return self._lines.pop(0)

    session._proc = type("P", (), {"stdout": _Stream()})()

    async def _boom(_msg: dict) -> None:
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(session, "_dispatch", _boom)
    await session._reader_loop()


@pytest.mark.asyncio
async def test_dispatch_fires_notification_listeners() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    fut = asyncio.get_event_loop().create_future()
    session._notification_listeners["session/update"] = [fut]
    await session._dispatch({"method": "session/update", "params": {"update": {}}})
    assert fut.result()["method"] == "session/update"


@pytest.mark.asyncio
async def test_stderr_loop_returns_when_stderr_missing() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    session._proc = type("P", (), {"stderr": None})()
    await session._stderr_loop()


def test_pick_resident_session_falls_back_to_any_advertised() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    session._advertised_sessions = {"only": {"resident": False}}
    assert session._pick_resident_session() == "only"


@pytest.mark.asyncio
async def test_discover_resident_session_times_out(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    monkeypatch.setattr("omnigent.inner.grok_native_executor.asyncio.sleep", _async_noop)
    sid = await session._discover_resident_session(timeout=0.0)
    assert sid is None


@pytest.mark.asyncio
async def test_start_process_spawns_reader_tasks(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model="grok-test")

    class _Proc:
        stdin = type("S", (), {"write": lambda *_a, **_k: None, "drain": _async_drain})()
        stdout = type("O", (), {"readline": _async_eof})()
        stderr = type("E", (), {"readline": _async_eof})()

    async def _fake_exec(*_argv, **_kwargs) -> _Proc:
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    await session._start_process(["grok", "agent", "stdio"], "/tmp")
    assert session._reader_task is not None


@pytest.mark.asyncio
async def test_prompt_session_streams_updates_before_done(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        rid = obj["id"]
        assert session._turn_q is not None
        session._turn_q.put_nowait(
            {"sessionUpdate": "agent_message_chunk", "content": {"text": "chunk"}}
        )
        session._pending[rid].set_result({"result": {}})

    monkeypatch.setattr(session, "_send", _fake_send)
    events = [event async for event in session._prompt_session("sess-1", "hi")]
    assert isinstance(events[0], TextChunk)
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_tui_start_initializes_leader_connection(monkeypatch) -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)

    async def _fake_start_process(argv: list[str], _cwd: str) -> None:
        session._proc = type("P", (), {})()
        assert "--leader-socket" in argv

    async def _fake_request(method: str, _params: dict, *, timeout: float | None) -> dict:
        assert method == "initialize"
        return {"result": {}}

    monkeypatch.setattr(session, "_start_process", _fake_start_process)
    monkeypatch.setattr(session, "_request", _fake_request)
    await session.start()
    assert session._initialized is True


@pytest.mark.asyncio
async def test_tui_prompt_starts_when_uninitialized(monkeypatch) -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)
    session._grok_session_id = "steady"

    async def _fake_start() -> None:
        session._initialized = True

    async def _fake_ensure_loaded(_sid: str) -> None:
        return None

    async def _fake_prompt_session(_sid: str, _text: str):
        yield TurnComplete(response=None)

    monkeypatch.setattr(session, "start", _fake_start)
    monkeypatch.setattr(session, "_ensure_loaded", _fake_ensure_loaded)
    monkeypatch.setattr(session, "_prompt_session", _fake_prompt_session)
    events = [event async for event in session.prompt("hi")]
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_ensure_loaded_swallows_load_errors(monkeypatch) -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)

    async def _boom(_method: str, _params: dict, *, timeout: float | None) -> dict:
        raise RuntimeError("already loaded")

    monkeypatch.setattr(session, "_request", _boom)
    await session._ensure_loaded("sess-1")
    assert session._loaded is True


@pytest.mark.asyncio
async def test_bootstrap_self_owned_session_missing_id(monkeypatch, tmp_path: Path) -> None:
    session = _GrokTuiAcpSession(
        leader_socket="/tmp/leader.sock",
        cwd="/tmp",
        bridge_dir=tmp_path,
    )

    async def _fake_inject(_text: str) -> bool:
        return False

    async def _fake_request(method: str, _params: dict, *, timeout: float | None) -> dict:
        if method == "session/new":
            return {"result": {}}
        return {"result": {}}

    monkeypatch.setattr(session, "_inject_into_tui", _fake_inject)
    monkeypatch.setattr(session, "_request", _fake_request)
    events = [event async for event in session._bootstrap_turn("hi")]
    assert isinstance(events[0], ExecutorError)


@pytest.mark.asyncio
async def test_inject_into_tui_swallows_bridge_errors(monkeypatch, tmp_path: Path) -> None:
    session = _GrokTuiAcpSession(
        leader_socket="/tmp/leader.sock",
        cwd="/tmp",
        bridge_dir=tmp_path,
    )

    def _boom(_dir, content: str) -> bool:
        raise RuntimeError("tmux down")

    monkeypatch.setattr("omnigent.grok_native_bridge.inject_user_message", _boom)
    assert await session._inject_into_tui("hi") is False


@pytest.mark.asyncio
async def test_read_tui_turn_waits_for_working_then_idle(monkeypatch) -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)
    session._advertised_sessions["sid-1"] = {"activity": "working"}
    queue = session._session_update_qs.setdefault("sid-1", asyncio.Queue())
    monkeypatch.setattr("omnigent.inner.grok_native_executor._TUI_TURN_TOKEN_TIMEOUT_S", 0.01)
    monkeypatch.setattr("omnigent.inner.grok_native_executor._TUI_TURN_MAX_S", 0.05)

    async def _flip_idle() -> None:
        await asyncio.sleep(0.02)
        session._advertised_sessions["sid-1"] = {"activity": "idle"}

    flip_idle_task = asyncio.create_task(_flip_idle())
    events = [event async for event in session._read_tui_turn("sid-1")]
    await flip_idle_task
    assert isinstance(events[-1], TurnComplete)
    assert queue.empty()


def test_persist_state_noop_without_bridge_dir() -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)
    session._persist_state("sid")  # must not raise


@pytest.mark.asyncio
async def test_tui_do_cancel_and_self_spawn_do_cancel(monkeypatch) -> None:
    tui = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)
    tui._grok_session_id = "grok-1"
    sent: list[dict] = []

    async def _capture_send(obj: dict) -> None:
        sent.append(obj)

    monkeypatch.setattr(tui, "_send", _capture_send)
    tui._proc = object()
    await tui.do_cancel()
    assert sent and sent[0]["method"] == "session/cancel"

    self_spawn = _GrokAcpSession(cwd="/tmp", model=None)
    self_spawn._session_id = "self-1"
    sent.clear()
    monkeypatch.setattr(self_spawn, "_send", _capture_send)
    self_spawn._proc = object()
    await self_spawn.do_cancel()
    assert sent and sent[0]["params"]["sessionId"] == "self-1"


@pytest.mark.asyncio
async def test_self_spawn_start_full_path(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model="grok-2")

    async def _fake_start_process(_argv: list[str], _cwd: str) -> None:
        session._proc = type("P", (), {})()

    async def _fake_request(method: str, _params: dict, *, timeout: float | None) -> dict:
        if method == "initialize":
            return {"result": {}}
        if method == "session/new":
            return {"result": {"sessionId": "new-sess"}}
        return {"result": {}}

    monkeypatch.setattr(session, "_start_process", _fake_start_process)
    monkeypatch.setattr(session, "_request", _fake_request)
    await session.start()
    assert session._session_id == "new-sess"


@pytest.mark.asyncio
async def test_interrupt_tui_session(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_GROK_LEADER_SOCKET", "/tmp/leader.sock")
    ex = GrokNativeExecutor()
    cancelled: list[str] = []

    async def _fake_cancel() -> None:
        cancelled.append("yes")

    assert ex._tui_session is not None
    monkeypatch.setattr(ex._tui_session, "do_cancel", _fake_cancel)
    await ex.interrupt_session("key")
    assert cancelled == ["yes"]


def test_latest_user_text_empty_blocks_and_none_content() -> None:
    assert _latest_user_text([{"role": "user", "content": "plain"}]) == "plain"
    assert _latest_user_text([{"role": "user", "content": [{"type": "text", "text": ""}]}]) == ""
    assert _latest_user_text([{"role": "user", "content": None}]) == ""


@pytest.mark.asyncio
async def test_wait_for_notification_cleans_listener_on_timeout() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    with pytest.raises(asyncio.TimeoutError):
        await session._wait_for_notification("session/update", timeout=0.01)
    assert session._notification_listeners.get("session/update", []) == []


@pytest.mark.asyncio
async def test_wait_for_notification_ignores_stale_listener_on_timeout() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    async def _clear_listeners_mid_wait() -> None:
        await asyncio.sleep(0.005)
        session._notification_listeners["session/update"] = []

    clear_listeners_task = asyncio.create_task(_clear_listeners_mid_wait())
    with pytest.raises(asyncio.TimeoutError):
        await session._wait_for_notification("session/update", timeout=0.02)
    await clear_listeners_task


@pytest.mark.asyncio
async def test_discover_resident_session_polls_until_deadline(monkeypatch) -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)
    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("omnigent.inner.grok_native_executor.asyncio.sleep", _record_sleep)
    sid = await session._discover_resident_session(timeout=0.25)
    assert sid is None
    assert sleeps


@pytest.mark.asyncio
async def test_close_handles_process_lookup_error() -> None:
    session = _GrokAcpSession(cwd="/tmp", model=None)

    class _Proc:
        returncode = None

        def terminate(self) -> None:
            raise ProcessLookupError

    session._proc = _Proc()
    await session.close()
    assert session._proc is None


@pytest.mark.asyncio
async def test_ensure_loaded_is_idempotent() -> None:
    session = _GrokTuiAcpSession(leader_socket="/tmp/leader.sock", cwd="/tmp", bridge_dir=None)
    session._loaded = True
    await session._ensure_loaded("sess-1")


@pytest.mark.asyncio
async def test_persist_state_swallows_write_errors(monkeypatch, tmp_path: Path) -> None:
    session = _GrokTuiAcpSession(
        leader_socket="/tmp/leader.sock",
        cwd="/tmp",
        bridge_dir=tmp_path,
    )

    def _boom(_bridge_dir, _state) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("omnigent.grok_native_bridge.write_bridge_state", _boom)
    session._persist_state("sid")  # must not raise


async def _async_noop() -> None:
    return None


async def _async_drain() -> None:
    return None


async def _async_eof() -> bytes:
    return b""
