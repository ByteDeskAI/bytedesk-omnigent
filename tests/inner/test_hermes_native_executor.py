"""Unit tests for the ``hermes`` harness (ACP-over-stdio bridge).

No live ``hermes`` binary: the subprocess/ACP frames are mocked. Covers the
SOUL.md projection, latest-user-text extraction, ACP→ExecutorEvent translation,
and harness registry resolution.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from bytedesk_omnigent.harnesses.config_apply import apply_spec_to_hermes
from bytedesk_omnigent.harnesses.hermes_native_executor import (
    _HermesAcpSession,
    _latest_user_text,
    _translate_update,
)
from omnigent.inner.executor import (
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)


# ── apply_spec_to_hermes (idempotent SOUL.md projection) ────────────────────


def test_apply_spec_writes_soul_and_version_first_call(tmp_path: Path) -> None:
    prompt = "You are Kade Vector."
    changed = apply_spec_to_hermes(prompt, hermes_home=tmp_path)

    assert changed is True
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == prompt
    assert (tmp_path / ".applied-version").read_text(encoding="utf-8").strip() != ""


def test_apply_spec_noop_on_unchanged_prompt(tmp_path: Path) -> None:
    prompt = "You are Kade Vector."
    assert apply_spec_to_hermes(prompt, hermes_home=tmp_path) is True

    soul = tmp_path / "SOUL.md"
    mtime_before = soul.stat().st_mtime_ns

    # Second call, same prompt → no rewrite.
    assert apply_spec_to_hermes(prompt, hermes_home=tmp_path) is False
    assert soul.stat().st_mtime_ns == mtime_before


def test_apply_spec_rewrites_on_changed_prompt(tmp_path: Path) -> None:
    assert apply_spec_to_hermes("v1 persona", hermes_home=tmp_path) is True
    assert apply_spec_to_hermes("v2 persona", hermes_home=tmp_path) is True
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "v2 persona"


# ── _latest_user_text ───────────────────────────────────────────────────────


def test_latest_user_text_plain_string() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ]
    assert _latest_user_text(messages) == "latest"


def test_latest_user_text_list_of_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "alpha"},
                {"type": "image", "url": "ignored"},
                {"type": "input_text", "text": "beta"},
            ],
        },
    ]
    assert _latest_user_text(messages) == "alpha\nbeta"


def test_latest_user_text_none_content() -> None:
    messages = [{"role": "user", "content": None}]
    assert _latest_user_text(messages) == ""


def test_latest_user_text_no_user_message() -> None:
    messages = [{"role": "assistant", "content": "only assistant"}]
    assert _latest_user_text(messages) == ""


# ── _translate_update (ACP session/update → ExecutorEvent) ───────────────────


async def _collect(update: dict, tool_names: dict[str, str]) -> list:
    return [event async for event in _translate_update(update, tool_names)]


async def test_translate_agent_message_chunk_to_text() -> None:
    events = await _collect(
        {"sessionUpdate": "agent_message_chunk", "content": {"text": "hello"}}, {}
    )
    assert len(events) == 1
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "hello"


async def test_translate_thought_chunk_to_reasoning() -> None:
    events = await _collect(
        {"sessionUpdate": "agent_thought_chunk", "content": {"text": "thinking"}}, {}
    )
    assert len(events) == 1
    assert isinstance(events[0], ReasoningChunk)
    assert events[0].delta == "thinking"


async def test_translate_tool_call_and_completion() -> None:
    tool_names: dict[str, str] = {}
    req = await _collect(
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "sql_query"},
        tool_names,
    )
    assert isinstance(req[0], ToolCallRequest)
    assert req[0].name == "sql_query"
    assert req[0].metadata["call_id"] == "c1"

    done = await _collect(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"},
        tool_names,
    )
    assert isinstance(done[0], ToolCallComplete)
    assert done[0].name == "sql_query"
    assert done[0].status is ToolCallStatus.SUCCESS


async def test_translate_failed_tool_call() -> None:
    tool_names = {"c2": "deploy"}
    done = await _collect(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "failed"},
        tool_names,
    )
    assert done[0].status is ToolCallStatus.ERROR


async def test_translate_unknown_kind_text_passthrough() -> None:
    events = await _collect(
        {"sessionUpdate": "some_future_kind", "content": {"text": "kept"}}, {}
    )
    assert len(events) == 1
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "kept"


async def test_translate_empty_text_yields_nothing() -> None:
    assert await _collect({"sessionUpdate": "agent_message_chunk", "content": {}}, {}) == []


# ── End-to-end prompt translation (reader mocked with canned frames) ─────────


async def test_prompt_session_streams_text_then_turn_complete(monkeypatch) -> None:
    """Feed canned ACP frames through _prompt_session; assert the event order."""
    session = _HermesAcpSession(cwd="/tmp", model=None)

    sent: list[dict] = []

    async def _fake_send(obj: dict) -> None:
        sent.append(obj)
        # When the session/prompt request is sent, enqueue the canned turn:
        # two text updates, then resolve the prompt response future so the
        # _signal_done task pushes the _TURN_DONE sentinel.
        if obj.get("method") == "session/prompt":
            rid = obj["id"]
            queue = session._turn_q
            assert queue is not None
            queue.put_nowait({"sessionUpdate": "agent_message_chunk", "content": {"text": "Hi "}})
            queue.put_nowait(
                {"sessionUpdate": "agent_message_chunk", "content": {"text": "there"}}
            )
            session._pending[rid].set_result({"jsonrpc": "2.0", "id": rid, "result": {}})

    monkeypatch.setattr(session, "_send", _fake_send)

    events = [event async for event in session._prompt_session("sess-1", "ping")]

    texts = [e.text for e in events if isinstance(e, TextChunk)]
    assert texts == ["Hi ", "there"]
    assert isinstance(events[-1], TurnComplete)

    # The prompt request carried the user text in ACP shape.
    prompt_req = next(o for o in sent if o.get("method") == "session/prompt")
    assert prompt_req["params"]["sessionId"] == "sess-1"
    assert prompt_req["params"]["prompt"] == [{"type": "text", "text": "ping"}]


async def test_prompt_session_surfaces_acp_error(monkeypatch) -> None:
    from omnigent.inner.executor import ExecutorError

    session = _HermesAcpSession(cwd="/tmp", model=None)

    async def _fake_send(obj: dict) -> None:
        if obj.get("method") == "session/prompt":
            rid = obj["id"]
            session._pending[rid].set_result(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -1, "message": "boom"}}
            )

    monkeypatch.setattr(session, "_send", _fake_send)

    events = [event async for event in session._prompt_session("sess-1", "ping")]
    assert isinstance(events[-1], ExecutorError)
    assert "boom" in events[-1].message


# ── Harness registry resolution ──────────────────────────────────────────────


def test_hermes_harness_registered_and_exposes_create_app() -> None:
    from omnigent.runtime.harnesses import _HARNESS_MODULES

    module_path = _HARNESS_MODULES["hermes"]
    assert module_path == "bytedesk_omnigent.harnesses.hermes_native_harness"

    module = importlib.import_module(module_path)
    assert callable(module.create_app)


def test_build_hermes_executor_respects_model_env(monkeypatch) -> None:
    from bytedesk_omnigent.harnesses import hermes_native_harness

    monkeypatch.delenv("HARNESS_HERMES_MODEL", raising=False)
    assert hermes_native_harness._build_hermes_native_executor()._model is None

    monkeypatch.setenv("HARNESS_HERMES_MODEL", "some-model")
    assert hermes_native_harness._build_hermes_native_executor()._model == "some-model"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
