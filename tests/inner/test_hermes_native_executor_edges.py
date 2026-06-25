"""Seam tests for HermesNativeExecutor and _HermesAcpSession lifecycle edges."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bytedesk_omnigent.harnesses.hermes_native_executor import (
    HermesNativeExecutor,
    _HermesAcpSession,
    _latest_user_text,
    _resolve_cwd,
    _translate_update,
)
from omnigent.inner.executor import (
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)


def test_latest_user_text_serializes_non_text_content() -> None:
    messages = [{"role": "user", "content": {"tool": "ping"}}]
    assert _latest_user_text(messages) == '{"tool": "ping"}'


def test_resolve_cwd_prefers_env_then_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARNESS_HERMES_CWD", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    assert _resolve_cwd() == str(tmp_path)

    monkeypatch.setenv("HOME", "/home/hermes")
    assert _resolve_cwd() == "/home/hermes"
    monkeypatch.setenv("HARNESS_HERMES_CWD", "/work")
    assert _resolve_cwd() == "/work"


@pytest.mark.asyncio
async def test_send_raises_when_process_missing() -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)
    with pytest.raises(RuntimeError, match="not running"):
        await session._send({"jsonrpc": "2.0"})


@pytest.mark.asyncio
async def test_request_round_trip_and_timeout(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        rid = obj["id"]
        session._pending[rid].set_result({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

    monkeypatch.setattr(session, "_send", _fake_send)
    resp = await session._request("ping", {}, timeout=1.0)
    assert resp["result"]["ok"] is True

    timeout_session = _HermesAcpSession(cwd="/tmp", model=None)

    async def _never_respond(_obj: dict) -> None:
        return None

    monkeypatch.setattr(timeout_session, "_send", _never_respond)
    with pytest.raises(asyncio.TimeoutError):
        await timeout_session._request("slow", {}, timeout=0.01)


@pytest.mark.asyncio
async def test_dispatch_handles_response_permission_and_fs() -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)
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
async def test_reader_and_stderr_loops_ignore_bad_lines(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)

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
async def test_start_requires_session_id(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model="m1")

    async def _fake_start_process(_argv: list[str], _cwd: str) -> None:
        session._proc = type(
            "P",
            (),
            {"stdin": type("S", (), {"write": lambda *_a, **_k: None, "drain": _async_noop})()},
        )()

    async def _fake_request(method: str, _params: dict, *, timeout: float | None) -> dict:
        if method == "session/new":
            return {"result": {}}
        return {"result": {}}

    monkeypatch.setattr(session, "_start_process", _fake_start_process)
    monkeypatch.setattr(session, "_request", _fake_request)
    with pytest.raises(RuntimeError, match="no sessionId"):
        await session.start()


@pytest.mark.asyncio
async def test_prompt_starts_session_and_surfaces_init_error(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)

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
async def test_cancel_and_close_are_idempotent() -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)
    await session.cancel()
    await session.close()

    class _Proc:
        returncode = None

        def terminate(self) -> None:
            self.returncode = 0

    session._proc = _Proc()
    session._session_id = "sess-1"
    session._reader_task = asyncio.create_task(asyncio.sleep(60))
    session._stderr_task = asyncio.create_task(asyncio.sleep(60))
    await session.cancel()
    await session.close()
    assert session._proc is None


@pytest.mark.asyncio
async def test_hermes_executor_capabilities_and_interrupt() -> None:
    ex = HermesNativeExecutor()
    assert ex.supports_streaming() is True
    assert ex.supports_tool_calling() is True
    assert ex.handles_tools_internally() is True
    assert await ex.interrupt_session("ignored") is True
    await ex.close()


@pytest.mark.asyncio
async def test_run_turn_no_user_input() -> None:
    ex = HermesNativeExecutor()
    events = [event async for event in ex.run_turn([], [], "persona")]
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no user input" in events[0].message


@pytest.mark.asyncio
async def test_run_turn_applies_soul_and_streams_prompt(monkeypatch, tmp_path: Path) -> None:
    ex = HermesNativeExecutor()

    async def _fake_prompt(text: str):
        assert text == "hello"
        yield TextChunk(text="reply")
        yield TurnComplete(response=None)

    monkeypatch.setattr(ex._session, "prompt", _fake_prompt)
    monkeypatch.setattr(
        "bytedesk_omnigent.harnesses.hermes_native_executor.apply_spec_to_hermes",
        lambda _prompt, hermes_home=None: True,
    )

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
async def test_run_turn_tolerates_soul_apply_failure_and_prompt_errors(monkeypatch) -> None:
    ex = HermesNativeExecutor()

    def _boom(_prompt: str, hermes_home=None) -> bool:
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        "bytedesk_omnigent.harnesses.hermes_native_executor.apply_spec_to_hermes",
        _boom,
    )

    async def _raise_prompt(_text: str):
        raise RuntimeError("pipe broken")
        yield  # pragma: no cover

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


async def _async_noop() -> None:
    return None


def test_latest_user_text_skips_non_text_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "image", "url": "x"}, {"type": "text", "text": ""}],
        }
    ]
    assert _latest_user_text(messages) == ""


@pytest.mark.asyncio
async def test_translate_tool_call_uses_kind_fallback() -> None:
    events = [
        event
        async for event in _translate_update(
            {"sessionUpdate": "tool_call", "toolCallId": "c3", "kind": "search"}, {}
        )
    ]
    assert isinstance(events[0], ToolCallRequest)
    assert events[0].name == "search"


@pytest.mark.asyncio
async def test_send_writes_to_process_stdin(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)
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
async def test_request_without_timeout(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        rid = obj["id"]
        session._pending[rid].set_result({"jsonrpc": "2.0", "id": rid, "result": {}})

    monkeypatch.setattr(session, "_send", _fake_send)
    resp = await session._request("ping", {}, timeout=None)
    assert resp["result"] == {}


@pytest.mark.asyncio
async def test_reader_loop_logs_dispatch_errors(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)

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
async def test_start_process_spawns_reader_tasks(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model="gpt-test")

    class _Proc:
        stdin = type("S", (), {"write": lambda *_a, **_k: None, "drain": _async_noop})()
        stdout = type("O", (), {"readline": _async_eof})()
        stderr = type("E", (), {"readline": _async_eof})()

    async def _fake_exec(*_argv, **_kwargs) -> _Proc:
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    async def _fake_requests(method: str, _params: dict, *, timeout: float | None) -> dict:
        if method == "session/new":
            return {"result": {"sessionId": "sess-99"}}
        return {"result": {}}

    monkeypatch.setattr(session, "_request", _fake_requests)
    await session._start_process(["hermes", "acp"], "/tmp")
    assert session._reader_task is not None
    await session.start()
    assert session._session_id == "sess-99"


@pytest.mark.asyncio
async def test_stderr_loop_returns_when_stderr_missing() -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)
    session._proc = type("P", (), {"stderr": None})()
    await session._stderr_loop()


@pytest.mark.asyncio
async def test_close_handles_missing_process(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)

    class _Proc:
        returncode = None

        def terminate(self) -> None:
            raise ProcessLookupError

    session._proc = _Proc()
    await session.close()
    assert session._proc is None


@pytest.mark.asyncio
async def test_cancel_swallows_send_errors(monkeypatch) -> None:
    session = _HermesAcpSession(cwd="/tmp", model=None)
    session._session_id = "sess-1"
    session._proc = type("P", (), {})()

    async def _boom(_obj: dict) -> None:
        raise RuntimeError("broken pipe")

    monkeypatch.setattr(session, "_send", _boom)
    await session.cancel()


async def _async_eof() -> bytes:
    return b""


@pytest.mark.asyncio
async def test_translate_update_all_event_kinds() -> None:
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
            {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"},
            tool_names,
        )
    ]
    assert isinstance(events[0], ToolCallComplete)

    events = [
        event
        async for event in _translate_update(
            {"sessionUpdate": "unknown_kind", "content": {"text": "passthrough"}},
            {},
        )
    ]
    assert isinstance(events[0], TextChunk)


@pytest.mark.asyncio
async def test_prompt_session_yields_error_on_rpc_failure(monkeypatch) -> None:
    from bytedesk_omnigent.harnesses import hermes_native_executor as hne

    session = _HermesAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        rid = obj["id"]
        fut = session._pending[rid]
        assert session._turn_q is not None
        session._turn_q.put_nowait(hne._TURN_DONE)
        fut.set_result({"error": {"code": -1, "message": "boom"}})

    monkeypatch.setattr(session, "_send", _fake_send)
    events = [event async for event in session._prompt_session("sess-1", "hi")]
    assert any(isinstance(e, ExecutorError) for e in events)


def test_latest_user_text_only_assistant_returns_empty() -> None:
    assert _latest_user_text([{"role": "assistant", "content": "nope"}]) == ""


def test_latest_user_text_empty_text_block_returns_empty_string() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
    assert _latest_user_text(messages) == ""


def test_latest_user_text_serializes_dict_content() -> None:
    messages = [{"role": "user", "content": {"k": 1}}]
    assert _latest_user_text(messages) == '{"k": 1}'


@pytest.mark.asyncio
async def test_prompt_session_streams_chunk_before_complete(monkeypatch) -> None:
    from bytedesk_omnigent.harnesses import hermes_native_executor as hne

    session = _HermesAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        rid = obj["id"]
        assert session._turn_q is not None
        session._turn_q.put_nowait(
            {"sessionUpdate": "agent_message_chunk", "content": {"text": "chunk"}}
        )
        session._turn_q.put_nowait(hne._TURN_DONE)
        session._pending[rid].set_result({"result": {}})

    monkeypatch.setattr(session, "_send", _fake_send)
    events = [event async for event in session._prompt_session("sess-1", "hi")]
    assert isinstance(events[0], TextChunk)
    assert isinstance(events[-1], TurnComplete)
