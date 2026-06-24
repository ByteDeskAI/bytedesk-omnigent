"""Edge-case unit coverage for :mod:`omnigent.runtime.harnesses._executor_adapter`."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
)
from omnigent.runtime.harnesses._executor_adapter import (
    ExecutorAdapter,
    _INNER_EXCEPTION_CHAIN,
    _bridge_one_dispatch,
    _call_id_from_metadata,
    _classify_claude_sdk_exception,
    _classify_httpx_exception,
    _classify_openai_exception,
    _extract_role_keyed_messages,
    _extract_user_text,
    _normalize_message_content,
    _normalize_tool_schemas,
    _serialize_tool_result,
    _stringify_tool_payload,
    classify_inner_exception,
)
from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload, TurnContext
from omnigent.server.schemas import (
    CreateResponseRequest,
    ElicitationResult,
    InjectionConsumedEvent,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    ReasoningStartedEvent,
    ReasoningSummaryTextDeltaEvent,
    ReasoningTextDeltaEvent,
)


class _StubExecutor:
    """Minimal executor for adapter construction."""

    async def close(self) -> None:
        """No-op."""

    async def close_session(self, session_key: str) -> None:
        """No-op."""
        del session_key


class _RecordingCtx:
    """Records :meth:`emit` calls."""

    def __init__(self, response_id: str = "resp_test") -> None:
        self.response_id = response_id
        self.emitted: list[Any] = []
        self.provider_usage: dict[str, Any] | None = None

    def emit(self, event: Any) -> None:
        self.emitted.append(event)


# ── run_turn streaming / error paths ───────────────────────────


class _GateExecutor(Executor):
    """Yields one chunk, blocks until released, then completes."""

    def __init__(self) -> None:
        self.interrupted: list[str] = []
        self._release = asyncio.Event()

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, system_prompt, config
        yield TextChunk(text="partial")
        await self._release.wait()
        yield TurnComplete(response="late")

    async def interrupt_session(self, session_key: str) -> bool:
        self.interrupted.append(session_key)
        return True


@pytest.mark.asyncio
async def test_run_turn_interrupts_inner_session_when_cancelled_mid_stream() -> None:
    """Cancellation between streamed events drops the inner session."""
    executor = _GateExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = TurnContext(response_id="resp_cancel", event_queue=asyncio.Queue(), cancelled=asyncio.Event())

    task = asyncio.create_task(
        adapter.run_turn(CreateResponseRequest(model="agent", input="hi"), ctx)
    )
    await asyncio.sleep(0.05)
    ctx.cancelled.set()
    executor._release.set()
    await task

    assert executor.interrupted == ["sk"]


class _ErrorYieldingExecutor(Executor):
    """Yields :class:`ExecutorError` mid-turn."""

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, system_prompt, config
        yield ExecutorError(message="inner boom")


@pytest.mark.asyncio
async def test_run_turn_raises_on_executor_error_event() -> None:
    """``ExecutorError`` from the inner executor becomes ``RuntimeError``."""
    adapter = ExecutorAdapter(executor_factory=lambda: _ErrorYieldingExecutor())
    ctx = TurnContext(response_id="resp_err", event_queue=asyncio.Queue(), cancelled=asyncio.Event())

    with pytest.raises(RuntimeError, match="inner executor error: inner boom"):
        await adapter.run_turn(CreateResponseRequest(model="agent", input="hi"), ctx)


class _ClosingExecutor(_StubExecutor):
    """Records shutdown cleanup."""

    def __init__(self) -> None:
        self.closed_sessions: list[str] = []
        self.closed = False

    async def close_session(self, session_key: str) -> None:
        self.closed_sessions.append(session_key)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_on_shutdown_closes_inner_executor() -> None:
    """``on_shutdown`` closes the session and executor."""
    executor = _ClosingExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk_shutdown")
    adapter._executor = executor  # type: ignore[assignment]

    await adapter.on_shutdown()

    assert executor.closed_sessions == ["sk_shutdown"]
    assert executor.closed is True
    assert adapter._executor is None


# ── _watch_injections edge paths ───────────────────────────────


class _NoneInjectionCtx:
    """Returns ``None`` from ``next_injection`` once."""

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()
        self.emitted: list[Any] = []

    async def next_injection(self, timeout: float | None = None) -> Any:
        del timeout
        return None

    def emit(self, event: Any) -> None:
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_watch_injections_returns_on_none_injection() -> None:
    """A ``None`` injection sentinel ends the watcher loop."""
    executor = _StubExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = _NoneInjectionCtx()

    await adapter._watch_injections(ctx, executor)  # type: ignore[arg-type]

    assert ctx.emitted == []


class _OneShotInjectionCtx:
    """Delivers a single injection then blocks."""

    def __init__(self, injection: Any) -> None:
        self._pending = injection
        self.cancelled = asyncio.Event()
        self.emitted: list[Any] = []

    async def next_injection(self, timeout: float | None = None) -> Any:
        del timeout
        if self._pending is None:
            await asyncio.sleep(3600)
        inj, self._pending = self._pending, None
        return inj

    def emit(self, event: Any) -> None:
        self.emitted.append(event)


class _FailingEnqueueExecutor:
    """``enqueue_session_message`` raises."""

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        del session_key, content
        raise RuntimeError("enqueue failed")


class _RefusingEnqueueExecutor:
    """``enqueue_session_message`` returns ``False``."""

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        del session_key, content
        return False


@pytest.mark.asyncio
async def test_watch_injections_skips_empty_text_payload() -> None:
    """Malformed injections with no text are skipped."""
    executor = _FailingEnqueueExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = _OneShotInjectionCtx(CreateResponseRequest(model="m", input=[]))

    task = asyncio.create_task(adapter._watch_injections(ctx, executor))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert ctx.emitted == []


@pytest.mark.asyncio
async def test_watch_injections_continues_after_enqueue_exception() -> None:
    """Enqueue failures are logged and the watcher keeps looping."""
    executor = _FailingEnqueueExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = _OneShotInjectionCtx(CreateResponseRequest(model="m", input="hello"))

    task = asyncio.create_task(adapter._watch_injections(ctx, executor))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert ctx.emitted == []


@pytest.mark.asyncio
async def test_watch_injections_continues_when_enqueue_refused() -> None:
    """A refused injection does not emit a consumed marker."""
    executor = _RefusingEnqueueExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = _OneShotInjectionCtx(
        CreateResponseRequest(model="m", input="steer", injection_id="inj_refused")
    )

    task = asyncio.create_task(adapter._watch_injections(ctx, executor))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert ctx.emitted == []


# ── stable bridge callbacks ────────────────────────────────────


@pytest.mark.asyncio
async def test_stable_tool_executor_without_active_context_returns_error() -> None:
    """Stale tool callbacks return an explicit error payload."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    result = await adapter._stable_tool_executor("some_tool", {"x": 1})
    assert result == {"error": "no active turn context for tool dispatch"}


class _ElicitingCtx:
    """Minimal ctx for elicitation tests."""

    def __init__(self, action: str) -> None:
        self.response_id = "resp_elicit"
        self._action = action

    async def elicit(self, elicitation_id: str, params: Any) -> ElicitationResult:
        del elicitation_id, params
        return ElicitationResult(action=self._action, content=None)


@pytest.mark.asyncio
async def test_stable_elicitation_handler_denies_without_active_context() -> None:
    """No active turn context → deny by default."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    assert await adapter._stable_elicitation_handler("Bash", {"command": "ls"}) is False


@pytest.mark.asyncio
async def test_stable_elicitation_handler_accepts_on_accept_action() -> None:
    """User approval returns ``True``."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    adapter._current_ctx = _ElicitingCtx("accept")  # type: ignore[assignment]
    assert await adapter._stable_elicitation_handler("Bash", {"command": "ls"}) is True


@pytest.mark.asyncio
async def test_stable_elicitation_handler_declines_on_non_accept_action() -> None:
    """User denial returns ``False``."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    adapter._current_ctx = _ElicitingCtx("decline")  # type: ignore[assignment]
    assert await adapter._stable_elicitation_handler("Bash", {"command": "ls"}) is False


@pytest.mark.asyncio
async def test_stable_elicitation_handler_uses_repr_for_non_json_tool_input() -> None:
    """Non-JSON-serializable tool input still produces a preview."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    adapter._current_ctx = _ElicitingCtx("accept")  # type: ignore[assignment]
    assert await adapter._stable_elicitation_handler("Bash", {object(): "x"}) is True


@pytest.mark.asyncio
async def test_stable_policy_evaluator_allows_without_active_context() -> None:
    """Policy evaluator without ctx defaults to ALLOW."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    verdict = await adapter._stable_policy_evaluator("PHASE_LLM_REQUEST", {"x": 1})
    assert verdict == PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")


class _PolicyCtx:
    """Returns a fixed policy verdict."""

    def __init__(self) -> None:
        self.response_id = "resp_policy"

    async def evaluate_policy(
        self, evaluation_id: str, phase: str, data: dict[str, Any]
    ) -> PolicyVerdictPayload:
        del evaluation_id, phase, data
        return PolicyVerdictPayload(action="POLICY_ACTION_DENY")


@pytest.mark.asyncio
async def test_stable_policy_evaluator_routes_through_active_context() -> None:
    """Active ctx forwards evaluation to the scaffold round-trip."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    adapter._current_ctx = _PolicyCtx()  # type: ignore[assignment]
    verdict = await adapter._stable_policy_evaluator("PHASE_LLM_RESPONSE", {"y": 2})
    assert verdict.action == "POLICY_ACTION_DENY"


# ── _translate_event branches ────────────────────────────────────


def test_translate_event_text_chunk_emits_output_delta() -> None:
    """``TextChunk`` maps to ``response.output_text.delta``."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingCtx()
    adapter._translate_event(TextChunk(text="hello"), ctx)  # type: ignore[arg-type]
    assert len(ctx.emitted) == 1
    assert isinstance(ctx.emitted[0], OutputTextDeltaEvent)
    assert ctx.emitted[0].delta == "hello"


@pytest.mark.parametrize(
    ("event_type", "expected_cls"),
    [
        ("reasoning_started", ReasoningStartedEvent),
        ("reasoning_summary", ReasoningSummaryTextDeltaEvent),
        ("reasoning_text", ReasoningTextDeltaEvent),
    ],
)
def test_translate_event_reasoning_chunk_variants(
    event_type: str,
    expected_cls: type,
) -> None:
    """Each reasoning flavor maps to the matching SSE event."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingCtx()
    adapter._translate_event(
        ReasoningChunk(delta="thought", event_type=event_type),
        ctx,  # type: ignore[arg-type]
    )
    assert len(ctx.emitted) == 1
    assert isinstance(ctx.emitted[0], expected_cls)


def test_translate_event_tool_call_complete_suppressed_with_active_ctx() -> None:
    """Dispatched tools suppress duplicate ``function_call_output`` emits."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    adapter._current_ctx = _RecordingCtx()  # type: ignore[assignment]
    ctx = _RecordingCtx()
    adapter._translate_event(
        ToolCallComplete(name="t", status=ToolCallStatus.SUCCESS, result="ok"),
        ctx,  # type: ignore[arg-type]
    )
    assert ctx.emitted == []


def test_translate_event_tool_call_complete_emits_output_without_active_ctx() -> None:
    """Observed-only completions emit ``function_call_output``."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    adapter._current_ctx = None
    ctx = _RecordingCtx()
    adapter._translate_event(
        ToolCallComplete(
            name="echo",
            status=ToolCallStatus.SUCCESS,
            result="done",
            metadata={"call_id": "call_1", "arguments": {"x": 1}},
        ),
        ctx,  # type: ignore[arg-type]
    )
    assert len(ctx.emitted) == 1
    item = ctx.emitted[0].item
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "call_1"
    assert item["arguments"] == {"x": 1}


def test_translate_event_turn_complete_captures_provider_usage() -> None:
    """``TurnComplete.usage`` is stored on the turn context."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingCtx()
    usage = {"input_tokens": 3, "output_tokens": 7}
    adapter._translate_event(TurnComplete(response=None, usage=usage), ctx)  # type: ignore[arg-type]
    assert ctx.provider_usage == usage


# ── error classification helpers ───────────────────────────────


def test_build_error_detail_uses_classified_semantic_code() -> None:
    """Known inner-SDK exceptions map to allowlist codes."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    detail = adapter._build_error_detail(httpx.ConnectError("refused"))
    assert detail.code == "connection_error"
    assert "refused" in detail.message


def test_build_error_detail_falls_back_to_exception_class_name() -> None:
    """Unrecognized exceptions preserve the class name for operators."""
    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    detail = adapter._build_error_detail(RuntimeError("mystery"))
    assert detail.code == "RuntimeError"
    assert "mystery" in detail.message


def test_classify_openai_exception_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``openai`` package returns ``None``."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "openai":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    assert _classify_openai_exception(RuntimeError("x")) is None


def test_classify_claude_sdk_exception_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``claude_agent_sdk`` returns ``None``."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "claude_agent_sdk":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    assert _classify_claude_sdk_exception(RuntimeError("x")) is None


def test_classify_httpx_exception_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``httpx`` returns ``None``."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "httpx":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    assert _classify_httpx_exception(RuntimeError("x")) is None


def test_classify_httpx_exception_maps_network_and_remote_protocol_errors() -> None:
    """Transport-level ``NetworkError`` and ``RemoteProtocolError`` retry."""
    read_err = httpx.ReadError("peer closed")
    remote = httpx.RemoteProtocolError("incomplete body")
    assert _classify_httpx_exception(read_err) == "connection_error"
    assert _classify_httpx_exception(remote) == "connection_error"


def test_inner_exception_chain_exposes_registered_classifiers() -> None:
    """``classifiers()`` returns the built-in chain in registration order."""
    names = [fn.__name__ for fn in _INNER_EXCEPTION_CHAIN.classifiers()]
    assert names == [
        "_classify_openai_exception",
        "_classify_anthropic_exception",
        "_classify_claude_sdk_exception",
        "_classify_httpx_exception",
    ]


def test_classify_inner_exception_context_length_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic context-overflow codes classify when SDK classifiers are absent."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("openai", "anthropic", "claude_agent_sdk"):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    class _OverflowError(Exception):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.code = "context_length_exceeded"

    assert classify_inner_exception(_OverflowError("too long")) == "context_length_exceeded"


# ── tool schema / message helpers ──────────────────────────────


def test_normalize_tool_schemas_skips_empty_flat_names() -> None:
    """Flat schemas with blank names are dropped."""
    result = _normalize_tool_schemas([{"name": ""}, {"name": "ok", "description": "d"}])
    assert result == [{"name": "ok", "description": "d"}]


def test_normalize_tool_schemas_translates_chat_completions_shape() -> None:
    """OpenAI chat-completions tool schemas flatten to inner shape."""
    result = _normalize_tool_schemas(
        [
            {"type": "function", "function": {"name": ""}},
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "find things",
                    "parameters": {"type": "object"},
                },
            },
        ]
    )
    assert result == [
        {
            "name": "search",
            "description": "find things",
            "parameters": {"type": "object"},
        }
    ]


def test_extract_user_text_concatenates_block_list() -> None:
    """List-shaped injection input joins ``text`` fields."""
    assert _extract_user_text("plain") == "plain"
    assert _extract_user_text([{"text": "a"}, {"text": "b"}]) == "a\nb"


def test_extract_role_keyed_messages_skips_non_dict_items() -> None:
    """Non-dict entries in history input are ignored."""
    messages = _extract_role_keyed_messages(
        [
            "not-a-dict",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        ]
    )
    assert messages == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, ""),
        ("raw", "raw"),
        (123, ""),
        ([{"type": "input_image", "image_url": "x"}], [{"type": "input_image", "image_url": "x"}]),
        ([{"type": "input_text", "text": "a"}, "skip", {"type": "input_text", "text": "b"}], "a\nb"),
    ],
)
def test_normalize_message_content_edge_shapes(content: Any, expected: Any) -> None:
    """Content normalization handles null, scalar, multimodal, and junk blocks."""
    assert _normalize_message_content(content) == expected


def test_serialize_tool_result_paths() -> None:
    """Result, error, and empty completion shapes stringify correctly."""
    assert _serialize_tool_result(
        ToolCallComplete(name="t", status=ToolCallStatus.SUCCESS, result="ok")
    ) == "ok"
    assert _serialize_tool_result(
        ToolCallComplete(name="t", status=ToolCallStatus.ERROR, error="boom")
    ) == "[error] boom"
    assert (
        _serialize_tool_result(
            ToolCallComplete(name="t", status=ToolCallStatus.SUCCESS, result=None, error=None)
        )
        == ""
    )


def test_stringify_tool_payload_covers_list_dict_and_fallback() -> None:
    """Tool payloads stringify across common SDK shapes."""
    assert _stringify_tool_payload("plain") == "plain"
    assert _stringify_tool_payload([{"text": "a"}, {"text": "b"}]) == "ab"
    assert _stringify_tool_payload([{"type": "image"}]) == '[{"type": "image"}]'
    assert _stringify_tool_payload([{"text": "a"}, 42]) == "a"
    assert _stringify_tool_payload({"k": 1}) == '{"k": 1}'

    class _Unserializable:
        def __repr__(self) -> str:
            return "<unserializable>"

    assert _stringify_tool_payload(_Unserializable()) == "<unserializable>"


def test_call_id_from_metadata_rejects_non_string_values() -> None:
    """Only string ``call_id`` values are accepted."""
    assert _call_id_from_metadata({"call_id": 42}) is None
    assert _call_id_from_metadata({"call_id": "call_abc"}) == "call_abc"


# ── _bridge_one_dispatch ───────────────────────────────────────


class _DispatchCtx:
    """Records dispatch_tool calls and returns a configured payload."""

    def __init__(self, *, output: str = "{}", raise_error: bool = False) -> None:
        self.response_id = "resp_dispatch"
        self.calls: list[dict[str, Any]] = []
        self._output = output
        self._raise = raise_error

    async def dispatch_tool(
        self,
        *,
        call_id: str,
        name: str,
        arguments: str,
        agent: str,
    ) -> str:
        self.calls.append(
            {"call_id": call_id, "name": name, "arguments": arguments, "agent": agent}
        )
        if self._raise:
            raise RuntimeError("dispatch failed")
        return self._output


@pytest.mark.asyncio
async def test_bridge_one_dispatch_allocates_call_id_when_missing() -> None:
    """Missing ``call_id`` gets a freshly allocated id."""
    ctx = _DispatchCtx()
    result = await _bridge_one_dispatch(ctx, "agent", "tool", {"x": 1})  # type: ignore[arg-type]
    assert result == {}
    assert ctx.calls[0]["call_id"].startswith("call_")


@pytest.mark.asyncio
async def test_bridge_one_dispatch_returns_error_on_dispatch_failure() -> None:
    """Dispatch exceptions become ``{"error": ...}`` payloads."""
    ctx = _DispatchCtx(raise_error=True)
    result = await _bridge_one_dispatch(
        ctx, "agent", "tool", {"x": 1}, call_id="call_fixed"
    )  # type: ignore[arg-type]
    assert result == {"error": "dispatch failed"}


@pytest.mark.asyncio
async def test_bridge_one_dispatch_wraps_non_json_output() -> None:
    """Non-JSON tool output is returned under ``result``."""
    ctx = _DispatchCtx(output="plain text")
    result = await _bridge_one_dispatch(
        ctx, "agent", "tool", {}, call_id="call_fixed"
    )  # type: ignore[arg-type]
    assert result == {"result": "plain text"}


@pytest.mark.asyncio
async def test_bridge_one_dispatch_wraps_json_non_dict_output() -> None:
    """JSON arrays are wrapped under ``result``."""
    ctx = _DispatchCtx(output='["a"]')
    result = await _bridge_one_dispatch(
        ctx, "agent", "tool", {}, call_id="call_fixed"
    )  # type: ignore[arg-type]
    assert result == {"result": ["a"]}


@pytest.mark.asyncio
async def test_bridge_one_dispatch_parses_json_dict_output() -> None:
    """JSON object outputs pass through as structured dicts."""
    ctx = _DispatchCtx(output='{"ok": true}')
    result = await _bridge_one_dispatch(
        ctx, "agent", "tool", {}, call_id="call_fixed"
    )  # type: ignore[arg-type]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_run_turn_wires_tool_executor_on_first_use() -> None:
    """First ``run_turn`` installs stable bridges on the inner executor."""

    class _BridgeCaptureExecutor(Executor):
        async def run_turn(
            self,
            messages: list[Message],
            tools: list[ToolSpec],
            system_prompt: str,
            config: ExecutorConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, system_prompt, config
            yield TurnComplete(response="ok")

    executor = _BridgeCaptureExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    ctx = TurnContext(response_id="resp_wire", event_queue=asyncio.Queue(), cancelled=asyncio.Event())
    await adapter.run_turn(CreateResponseRequest(model="agent", input="hi"), ctx)

    assert executor._tool_executor == adapter._stable_tool_executor  # type: ignore[attr-defined]
    assert executor._elicitation_handler == adapter._stable_elicitation_handler  # type: ignore[attr-defined]
    assert executor._policy_evaluator == adapter._stable_policy_evaluator  # type: ignore[attr-defined]
    assert adapter._current_ctx is None
    assert adapter._current_agent is None


@pytest.mark.asyncio
async def test_watch_injections_emits_consumed_marker_with_injection_id() -> None:
    """Accepted injections with ``injection_id`` echo ``injection.consumed``."""
    class _AcceptExecutor:
        async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
            del session_key
            return True

    executor = _AcceptExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = _OneShotInjectionCtx(
        CreateResponseRequest(model="m", input="steer", injection_id="inj_ok")
    )

    task = asyncio.create_task(adapter._watch_injections(ctx, executor))  # type: ignore[arg-type]
    for _ in range(50):
        if ctx.emitted:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(ctx.emitted) == 1
    marker = ctx.emitted[0]
    assert isinstance(marker, InjectionConsumedEvent)
    assert marker.injection_id == "inj_ok"