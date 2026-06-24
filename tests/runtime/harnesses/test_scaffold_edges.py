"""Unit-level edge coverage for :mod:`omnigent.runtime.harnesses._scaffold`."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.harnesses import _scaffold as scaffold_mod
from omnigent.runtime.harnesses._scaffold import (
    ApprovalEvent,
    HarnessApp,
    InterruptEvent,
    MessageEvent,
    PolicyVerdictEvent,
    PolicyVerdictPayload,
    ToolResultEvent,
    TurnContext,
    _format_sse_event,
    _handle_omnigent_error,
    _health,
    _utc_now_iso,
)
from omnigent.server.schemas import (
    CreateResponseRequest,
    ElicitationRequestParams,
    ElicitationResult,
    HeartbeatEvent,
    OutputTextDeltaEvent,
)


class _EchoApp(HarnessApp):
    """Minimal harness for HTTP-level scaffold tests."""

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="ok"))


class _SlowShutdownApp(_EchoApp):
    """Keeps an in-flight turn registered briefly during drain."""

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        await asyncio.sleep(0.2)
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="slow"))


def test_message_event_to_create_request_maps_fields() -> None:
    """``MessageEvent`` adapts ``content`` to ``input`` and forwards extras."""
    event = MessageEvent(
        type="message",
        role="user",
        model="agent-a",
        content="hello",
        previous_response_id="resp_prev",
        instructions="be helpful",
    )
    req = event.to_create_request()
    assert req.model == "agent-a"
    assert req.input == "hello"
    assert req.previous_response_id == "resp_prev"
    assert req.instructions == "be helpful"


def test_approval_event_to_elicitation_result() -> None:
    """``ApprovalEvent`` adapts onto :class:`ElicitationResult``."""
    event = ApprovalEvent(
        type="approval",
        elicitation_id="elicit_1",
        action="accept",
        content={"field": "value"},
    )
    result = event.to_elicitation_result()
    assert result.action == "accept"
    assert result.content == {"field": "value"}


@pytest.mark.asyncio
async def test_turn_context_emit_resets_idle_watchdog() -> None:
    """Non-heartbeat emits invoke the idle-watchdog reset hook."""
    resets: list[str] = []

    def _reset() -> None:
        resets.append("tick")

    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext("resp_idle", queue, asyncio.Event())
    ctx._reset_idle_watchdog = _reset
    ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="x"))
    ctx.emit(HeartbeatEvent(type="response.heartbeat"))
    assert resets == ["tick"]


@pytest.mark.asyncio
async def test_turn_context_dispatch_tool_round_trip() -> None:
    """``dispatch_tool`` parks until ``_complete_tool`` resolves the Future."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext("resp_tool", queue, asyncio.Event())
    task = asyncio.create_task(
        ctx.dispatch_tool("call_1", "tool_name", "{}", agent="agent")
    )
    await asyncio.sleep(0)
    assert ctx._complete_tool("call_1", "payload")
    result = await task
    assert result == "payload"
    statuses: list[str] = []
    while not queue.empty():
        item = queue.get_nowait()
        if hasattr(item, "item") and item.item.get("type") == "function_call":
            statuses.append(item.item["status"])
    assert "action_required" in statuses
    assert "completed" in statuses


@pytest.mark.asyncio
async def test_turn_context_elicit_round_trip() -> None:
    """``elicit`` parks until ``_complete_elicitation`` resolves."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext("resp_elicit", queue, asyncio.Event())
    params = ElicitationRequestParams(mode="form", message="approve?")
    task = asyncio.create_task(ctx.elicit("elicit_1", params))
    await asyncio.sleep(0)
    assert ctx._complete_elicitation("elicit_1", ElicitationResult(action="decline"))
    result = await task
    assert result.action == "decline"


@pytest.mark.asyncio
async def test_next_injection_timeout_returns_none() -> None:
    """``next_injection`` returns ``None`` when the deadline elapses."""
    ctx = TurnContext("resp_inj", asyncio.Queue(), asyncio.Event())
    assert await ctx.next_injection(timeout=0.01) is None


@pytest.mark.asyncio
async def test_next_injection_without_timeout_blocks() -> None:
    """``next_injection`` without a timeout waits for an injection."""
    ctx = TurnContext("resp_inj2", asyncio.Queue(), asyncio.Event())
    req = CreateResponseRequest(model="m", input=[])
    task = asyncio.create_task(ctx.next_injection())
    await asyncio.sleep(0)
    ctx._push_injection(req)
    assert await task is req


@pytest.mark.asyncio
async def test_complete_tool_and_elicitation_stale_returns_false() -> None:
    """Stale ids on complete helpers are silent no-ops."""
    ctx = TurnContext("resp_stale", asyncio.Queue(), asyncio.Event())
    assert ctx._complete_tool("missing", "out") is False
    assert ctx._complete_elicitation("missing", ElicitationResult(action="cancel")) is False


@pytest.mark.asyncio
async def test_push_injection_enqueues_request() -> None:
    """``_push_injection`` places a request on the injection queue."""
    ctx = TurnContext("resp_push", asyncio.Queue(), asyncio.Event())
    req = CreateResponseRequest(model="m", input="x")
    ctx._push_injection(req)
    assert ctx._injection_queue.get_nowait() is req


@pytest.mark.asyncio
async def test_base_run_turn_raises_not_implemented() -> None:
    """The scaffold base class does not implement ``run_turn``."""
    with pytest.raises(NotImplementedError):
        await HarnessApp().run_turn(
            CreateResponseRequest(model="m", input=[]),
            TurnContext("resp_base", asyncio.Queue(), asyncio.Event()),
        )


def test_build_error_detail_uses_exception_type() -> None:
    """Default error detail maps the exception class name to the code."""
    detail = HarnessApp()._build_error_detail(ValueError("bad"))
    assert detail.code == "ValueError"
    assert detail.message == "bad"


def test_build_mounts_health_and_events_routes() -> None:
    """``build`` wires health + session events routes."""
    app = _EchoApp().build()
    assert isinstance(app, FastAPI)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/health" in paths


def test_check_conversation_id_errors() -> None:
    """Conversation binding mismatches surface as Omnigent errors."""
    app = _EchoApp().build()
    app.state.conversation_id = "conv_bound"

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/sessions/conv_other/events",
        "headers": [],
        "app": app,
    }
    request = Request(scope, _receive)

    harness = _EchoApp()
    with pytest.raises(OmnigentError, match="not served by this harness") as exc:
        harness._check_conversation_id(request, "conv_other")
    assert exc.value.code == ErrorCode.NOT_FOUND

    bare_app = _EchoApp().build()
    bare_scope = {**scope, "app": bare_app}
    bare_request = Request(bare_scope, _receive)
    with pytest.raises(OmnigentError, match="no conversation_id bound") as exc2:
        harness._check_conversation_id(bare_request, "conv_any")
    assert exc2.value.code == ErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_on_shutdown_signal_is_idempotent() -> None:
    """A second shutdown signal is ignored once shutdown has started."""
    app = _EchoApp()
    ctx = TurnContext("resp_sd", asyncio.Queue(), asyncio.Event())
    app._in_flight["resp_sd"] = ctx
    app._on_shutdown_signal()
    assert app._shutting_down.is_set()
    assert ctx.cancelled.is_set()
    app._on_shutdown_signal()


@pytest.mark.asyncio
async def test_drain_for_shutdown_waits_for_in_flight() -> None:
    """Drain loops until ``_in_flight`` clears or the grace elapses."""
    app = _EchoApp()
    app._in_flight["resp_drain"] = TurnContext("resp_drain", asyncio.Queue(), asyncio.Event())

    async def _clear_later() -> None:
        await asyncio.sleep(0.05)
        app._in_flight.clear()

    asyncio.create_task(_clear_later())
    await app._drain_for_shutdown()
    assert not app._in_flight


@pytest.mark.asyncio
async def test_teardown_turn_cancels_pending_run_task() -> None:
    """Teardown cancels a still-running ``run_turn`` task defensively."""
    app = _EchoApp()
    ctx = TurnContext("resp_teardown", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx.response_id] = ctx
    app._active_turn_ctx = ctx

    async def _hang() -> None:
        await asyncio.sleep(60)

    run_task = asyncio.create_task(_hang())
    heartbeat_task = asyncio.create_task(asyncio.sleep(60))
    await app._teardown_turn(ctx, run_task, heartbeat_task)
    assert run_task.cancelled() or run_task.done()
    assert ctx.response_id not in app._in_flight
    assert app._active_turn_ctx is None


@pytest.mark.asyncio
async def test_guarded_run_turn_absolute_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absolute turn ceiling surfaces as ``response.failed`` via RuntimeError."""
    monkeypatch.setattr(scaffold_mod, "_TURN_IDLE_TIMEOUT_S", 0)
    monkeypatch.setattr(scaffold_mod, "_TURN_ABSOLUTE_TIMEOUT_S", 0.05)

    class _BusyApp(HarnessApp):
        async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
            del request
            while True:
                ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="x"))
                await asyncio.sleep(0.02)

    app = _BusyApp()
    ctx = TurnContext("resp_abs", asyncio.Queue(), asyncio.Event())
    with pytest.raises(RuntimeError, match="absolute watchdog"):
        await app._guarded_run_turn(CreateResponseRequest(model="m", input=[]), ctx)
    drained: list[Any] = []
    while not ctx._event_queue.empty():
        drained.append(ctx._event_queue.get_nowait())
    assert drained[-1] is None


@pytest.mark.asyncio
async def test_guarded_run_turn_propagates_inner_timeout() -> None:
    """Inner ``TimeoutError`` that is not from harness watchdogs propagates."""

    class _InnerTimeoutApp(HarnessApp):
        async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
            del request, ctx
            raise TimeoutError("inner")

    app = _InnerTimeoutApp()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(scaffold_mod, "_TURN_IDLE_TIMEOUT_S", 0)
    monkeypatch.setattr(scaffold_mod, "_TURN_ABSOLUTE_TIMEOUT_S", 0)
    ctx = TurnContext("resp_inner", asyncio.Queue(), asyncio.Event())
    with pytest.raises(TimeoutError, match="inner"):
        await app._guarded_run_turn(CreateResponseRequest(model="m", input=[]), ctx)
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_heartbeat_loop_emits_on_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heartbeat loop enqueues keep-alive events until cancelled."""
    monkeypatch.setattr(scaffold_mod, "_HEARTBEAT_INTERVAL_S", 0.01)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext("resp_hb", queue, asyncio.Event())
    task = asyncio.create_task(HarnessApp()._heartbeat_loop(ctx))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert any(isinstance(item, HeartbeatEvent) for item in list(queue._queue))  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_build_terminal_event_completed_with_usage_model() -> None:
    """Completed turns carry provider usage including the pricing model field."""
    ctx = TurnContext("resp_usage", asyncio.Queue(), asyncio.Event())
    ctx.provider_usage = {
        "input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 3,
        "model": "claude-sonnet",
    }

    async def _ok() -> None:
        return None

    task = asyncio.create_task(_ok())
    await task
    terminal = await HarnessApp()._build_terminal_event(ctx, "agent", task, sequence=1)
    assert terminal.type == "response.completed"
    assert terminal.response.usage is not None
    assert terminal.response.usage.model == "claude-sonnet"


@pytest.mark.asyncio
async def test_build_terminal_event_run_task_exception_cancelled_race() -> None:
    """``run_task.exception()`` cancellation race yields ``response.cancelled``."""

    async def _cancelled_task() -> None:
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    ctx = TurnContext("resp_cancel_exc", asyncio.Queue(), asyncio.Event())
    task = asyncio.create_task(_cancelled_task())
    with contextlib.suppress(asyncio.CancelledError):
        await task
    terminal = await HarnessApp()._build_terminal_event(ctx, "agent", task, sequence=2)
    assert terminal.type == "response.cancelled"


def test_initial_envelope_events_shape() -> None:
    """Initial envelope emits created + in_progress with sequential numbers."""
    ctx = TurnContext("resp_env", asyncio.Queue(), asyncio.Event())
    events = HarnessApp()._initial_envelope_events(ctx, model="agent", start_seq=4)
    assert len(events) == 2
    assert events[0].sequence_number == 4
    assert events[1].sequence_number == 5
    assert events[0].response.id == "resp_env"


@pytest.mark.asyncio
async def test_resolve_elicitation_404_when_unknown() -> None:
    """Unknown elicitation ids return 404."""
    app = _EchoApp()
    with pytest.raises(OmnigentError, match="no outstanding elicitation"):
        await app._resolve_elicitation("missing", ElicitationResult(action="cancel"))


@pytest.mark.asyncio
async def test_handle_policy_verdict_event_resolves_in_flight() -> None:
    """Policy verdict events resolve the matching in-flight evaluation."""
    app = _EchoApp()
    ctx = TurnContext("resp_pv", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx.response_id] = ctx
    eval_task = asyncio.create_task(
        ctx.evaluate_policy("poleval_edge", "PHASE_LLM_REQUEST", {})
    )
    await asyncio.sleep(0)
    body = PolicyVerdictEvent(
        type="policy_verdict",
        evaluation_id="poleval_edge",
        action="POLICY_ACTION_ALLOW",
    )
    resp = await app._handle_policy_verdict_event(body)
    assert resp.status_code == 204
    result = await asyncio.wait_for(eval_task, timeout=2)
    assert result.action == "POLICY_ACTION_ALLOW"


@pytest.mark.asyncio
async def test_handle_tool_result_stale_is_noop() -> None:
    """Stale tool-result events return 204 without raising."""
    app = _EchoApp()
    resp = await app._handle_tool_result_event(
        ToolResultEvent(type="tool_result", call_id="stale", output="x")
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_post_session_event_unsupported_type_fails_loud() -> None:
    """Unsupported inbound event types raise instead of silently no-oping."""

    class _UnsupportedEvent:
        type = "unsupported"

    app = _EchoApp()
    built = app.build()
    built.state.conversation_id = "conv_x"
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "app": built}

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope, _receive)
    with pytest.raises(OmnigentError, match="unsupported inbound event"):
        await app._post_session_event("conv_x", _UnsupportedEvent(), request)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_start_or_inject_turn_refuses_when_shutting_down() -> None:
    """Fresh turns are refused with 503 once shutdown has begun."""
    app = _EchoApp()
    app._shutting_down.set()
    with pytest.raises(OmnigentError, match="shutting down"):
        await app._start_or_inject_turn(CreateResponseRequest(model="m", input=[]))


def test_lifespan_invokes_shutdown_hooks() -> None:
    """Lifespan teardown runs shutdown + drain hooks."""
    calls: list[str] = []

    class _TrackingApp(_EchoApp):
        async def on_shutdown(self) -> None:
            calls.append("on_shutdown")

        def _on_shutdown_signal(self) -> None:
            calls.append("signal")
            super()._on_shutdown_signal()

    app = _TrackingApp().build()
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
    assert calls == ["signal", "on_shutdown"]


def test_format_sse_event_and_helpers() -> None:
    """SSE formatting and small helpers produce the contract wire shape."""
    event = OutputTextDeltaEvent(type="response.output_text.delta", delta="hi", sequence_number=1)
    frame = _format_sse_event(event)
    assert frame.startswith(b"event: response.output_text.delta\n")
    assert b'"delta":"hi"' in frame
    assert _utc_now_iso().endswith("Z")


@pytest.mark.asyncio
async def test_health_and_omnigent_error_handlers() -> None:
    """Module-level handlers return the expected response shapes."""
    assert await _health() == {"status": "ok"}
    exc = OmnigentError("nope", code=ErrorCode.NOT_FOUND)
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    request = StarletteRequest(scope)
    response = await _handle_omnigent_error(request, exc)
    assert response.status_code == 404
    assert response.body is not None


def _session_request(app: FastAPI, conversation_id: str = "conv_x") -> Request:
    """Build a minimal FastAPI request bound to *app* state."""

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    app.state.conversation_id = conversation_id
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/v1/sessions/{conversation_id}/events",
        "headers": [],
        "app": app,
    }
    return Request(scope, _receive)


@pytest.mark.asyncio
async def test_cancel_pending_cancels_tool_and_elicitation_futures() -> None:
    """``_cancel_pending`` cancels outstanding tool and elicitation Futures."""
    ctx = TurnContext("resp_cancel", asyncio.Queue(), asyncio.Event())
    loop = asyncio.get_running_loop()
    tool_future = loop.create_future()
    elicit_future = loop.create_future()
    ctx._pending_tool_calls["call_cancel"] = tool_future
    ctx._pending_elicitations["elicit_cancel"] = elicit_future
    ctx._cancel_pending()
    assert tool_future.cancelled()
    assert elicit_future.cancelled()


@pytest.mark.asyncio
async def test_handle_interrupt_event_cancels_in_flight_turn() -> None:
    """Interrupt sets cancelled, clears inject target, and cancels pending work."""
    app = _EchoApp()
    ctx = TurnContext("resp_interrupt", asyncio.Queue(), asyncio.Event())
    loop = asyncio.get_running_loop()
    ctx._pending_tool_calls["call_int"] = loop.create_future()
    app._in_flight[ctx.response_id] = ctx
    app._active_turn_ctx = ctx
    resp = await app._handle_interrupt_event()
    assert resp.status_code == 204
    assert ctx.cancelled.is_set()
    assert ctx._pending_tool_calls["call_int"].cancelled()
    assert app._active_turn_ctx is None


@pytest.mark.asyncio
async def test_handle_interrupt_event_404_when_idle() -> None:
    """Interrupt with no in-flight turn surfaces as 404."""
    app = _EchoApp()
    with pytest.raises(OmnigentError, match="no in-flight turn"):
        await app._handle_interrupt_event()


@pytest.mark.asyncio
async def test_handle_tool_result_resolves_matching_call() -> None:
    """Tool-result events resolve the parked ``dispatch_tool`` Future."""
    app = _EchoApp()
    ctx = TurnContext("resp_tool_res", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx.response_id] = ctx
    dispatch_task = asyncio.create_task(
        ctx.dispatch_tool("call_match", "tool_name", "{}", agent="agent")
    )
    await asyncio.sleep(0)
    resp = await app._handle_tool_result_event(
        ToolResultEvent(type="tool_result", call_id="call_match", output="done")
    )
    assert resp.status_code == 204
    assert await dispatch_task == "done"


@pytest.mark.asyncio
async def test_resolve_elicitation_success() -> None:
    """Known elicitation ids resolve the parked Future with 204."""
    app = _EchoApp()
    ctx = TurnContext("resp_elicit_ok", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx.response_id] = ctx
    params = ElicitationRequestParams(mode="form", message="approve?")
    elicit_task = asyncio.create_task(ctx.elicit("elicit_ok", params))
    await asyncio.sleep(0)
    resp = await app._resolve_elicitation(
        "elicit_ok", ElicitationResult(action="accept", content={"ok": True})
    )
    assert resp.status_code == 204
    result = await elicit_task
    assert result.action == "accept"


@pytest.mark.asyncio
async def test_start_or_inject_turn_injects_via_previous_response_id() -> None:
    """``previous_response_id`` matching an in-flight turn enqueues injection."""
    app = _EchoApp()
    ctx = TurnContext("resp_prev_inj", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx.response_id] = ctx
    req = CreateResponseRequest(
        model="m", input="steer", previous_response_id=ctx.response_id
    )
    resp = await app._start_or_inject_turn(req)
    assert resp.status_code == 204
    assert ctx._injection_queue.get_nowait().input == "steer"


@pytest.mark.asyncio
async def test_start_or_inject_turn_injects_via_active_turn() -> None:
    """Sessions-native steering injects when ``_active_turn_ctx`` is set."""
    app = _EchoApp()
    ctx = TurnContext("resp_active_inj", asyncio.Queue(), asyncio.Event())
    app._active_turn_ctx = ctx
    req = CreateResponseRequest(model="m", input="steer")
    resp = await app._start_or_inject_turn(req)
    assert resp.status_code == 204
    assert ctx._injection_queue.get_nowait().input == "steer"


@pytest.mark.asyncio
async def test_start_or_inject_turn_starts_streaming_turn() -> None:
    """Fresh turns allocate a response id and return SSE streaming."""
    app = _EchoApp()
    resp = await app._start_or_inject_turn(CreateResponseRequest(model="agent", input="hi"))
    assert isinstance(resp, StreamingResponse)
    assert len(app._in_flight) == 1
    ctx = next(iter(app._in_flight.values()))
    assert app._active_turn_ctx is ctx
    frames: list[bytes] = []
    async for chunk in resp.body_iterator:  # type: ignore[attr-defined]
        frames.append(chunk)
    joined = b"".join(frames)
    assert b"response.created" in joined
    assert b"response.completed" in joined
    assert ctx.response_id not in app._in_flight
    assert app._active_turn_ctx is None


@pytest.mark.asyncio
async def test_stream_turn_emits_sequence_and_terminal_event() -> None:
    """``_stream_turn`` drives envelope, body events, and terminal teardown."""
    app = _EchoApp()
    ctx = TurnContext("resp_stream", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx.response_id] = ctx
    app._active_turn_ctx = ctx
    frames: list[bytes] = []
    async for chunk in app._stream_turn(
        CreateResponseRequest(model="agent", input="hi"), ctx
    ):
        frames.append(chunk)
    joined = b"".join(frames)
    assert b"response.created" in joined
    assert b"response.output_text.delta" in joined
    assert b"response.completed" in joined
    assert ctx.response_id not in app._in_flight


@pytest.mark.asyncio
async def test_stream_turn_stamps_heartbeat_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeats yielded from ``_stream_turn`` carry ``server_time`` and ``last_event_seq``."""

    class _BriefPauseApp(HarnessApp):
        async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
            del request
            await asyncio.sleep(0.03)
            ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="x"))

    monkeypatch.setattr(scaffold_mod, "_HEARTBEAT_INTERVAL_S", 0.01)
    app = _BriefPauseApp()
    ctx = TurnContext("resp_hb_stream", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx.response_id] = ctx
    frames: list[bytes] = []
    async for chunk in app._stream_turn(
        CreateResponseRequest(model="agent", input="hi"), ctx
    ):
        frames.append(chunk)
    heartbeat_frames = [frame for frame in frames if b"response.heartbeat" in frame]
    assert heartbeat_frames, "expected at least one heartbeat during a brief pause turn"
    assert b'"server_time"' in heartbeat_frames[0]
    assert b'"last_event_seq"' in heartbeat_frames[0]


@pytest.mark.asyncio
async def test_post_session_event_routes_message_interrupt_tool_and_approval() -> None:
    """``_post_session_event`` dispatches each supported inbound variant."""
    app = _EchoApp()
    built = app.build()
    request = _session_request(built)

    message_resp = await app._post_session_event(
        "conv_x",
        MessageEvent(type="message", role="user", model="agent", content="hi"),
        request,
    )
    assert isinstance(message_resp, StreamingResponse)

    ctx = TurnContext("resp_route", asyncio.Queue(), asyncio.Event())
    loop = asyncio.get_running_loop()
    ctx._pending_tool_calls["call_route"] = loop.create_future()
    app._in_flight[ctx.response_id] = ctx
    app._active_turn_ctx = ctx

    interrupt_resp = await app._post_session_event(
        "conv_x", InterruptEvent(type="interrupt"), request
    )
    assert interrupt_resp.status_code == 204

    ctx2 = TurnContext("resp_tool_route", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx2.response_id] = ctx2
    dispatch_task = asyncio.create_task(
        ctx2.dispatch_tool("call_route2", "tool", "{}", agent="agent")
    )
    await asyncio.sleep(0)
    tool_resp = await app._post_session_event(
        "conv_x",
        ToolResultEvent(type="tool_result", call_id="call_route2", output="ok"),
        request,
    )
    assert tool_resp.status_code == 204
    assert await dispatch_task == "ok"

    ctx3 = TurnContext("resp_appr_route", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx3.response_id] = ctx3
    params = ElicitationRequestParams(mode="form", message="go?")
    elicit_task = asyncio.create_task(ctx3.elicit("elicit_route", params))
    await asyncio.sleep(0)
    approval_resp = await app._post_session_event(
        "conv_x",
        ApprovalEvent(
            type="approval",
            elicitation_id="elicit_route",
            action="accept",
            content=None,
        ),
        request,
    )
    assert approval_resp.status_code == 204
    assert (await elicit_task).action == "accept"

    ctx4 = TurnContext("resp_pv_route", asyncio.Queue(), asyncio.Event())
    app._in_flight[ctx4.response_id] = ctx4
    eval_task = asyncio.create_task(
        ctx4.evaluate_policy("poleval_route", "PHASE_LLM_REQUEST", {})
    )
    await asyncio.sleep(0)
    verdict_resp = await app._post_session_event(
        "conv_x",
        PolicyVerdictEvent(
            type="policy_verdict",
            evaluation_id="poleval_route",
            action="POLICY_ACTION_ALLOW",
        ),
        request,
    )
    assert verdict_resp.status_code == 204
    assert (await eval_task).action == "POLICY_ACTION_ALLOW"


@pytest.mark.asyncio
async def test_guarded_run_turn_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idle watchdog fires when ``run_turn`` emits nothing for the window."""

    class _SilentApp(HarnessApp):
        async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
            del request, ctx
            await asyncio.sleep(0.2)

    monkeypatch.setattr(scaffold_mod, "_TURN_IDLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(scaffold_mod, "_TURN_ABSOLUTE_TIMEOUT_S", 10)
    app = _SilentApp()
    ctx = TurnContext("resp_idle", asyncio.Queue(), asyncio.Event())
    with pytest.raises(RuntimeError, match="idle watchdog"):
        await app._guarded_run_turn(CreateResponseRequest(model="m", input=[]), ctx)


@pytest.mark.asyncio
async def test_guarded_run_turn_propagates_cancelled_error() -> None:
    """Cancelled ``run_turn`` re-raises ``CancelledError`` for terminal synthesis."""

    class _CancelledApp(HarnessApp):
        async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
            del request, ctx
            raise asyncio.CancelledError

    app = _CancelledApp()
    ctx = TurnContext("resp_cancelled", asyncio.Queue(), asyncio.Event())
    with pytest.raises(asyncio.CancelledError):
        await app._guarded_run_turn(CreateResponseRequest(model="m", input=[]), ctx)


@pytest.mark.asyncio
async def test_build_terminal_event_handles_exception_cancelled_race() -> None:
    """``run_task.exception()`` raising ``CancelledError`` is treated as clean completion."""
    ctx = TurnContext("resp_exc_race", asyncio.Queue(), asyncio.Event())

    class _RaceTask:
        def done(self) -> bool:
            return True

        def cancelled(self) -> bool:
            return False

        def exception(self) -> BaseException | None:
            raise asyncio.CancelledError

    terminal = await HarnessApp()._build_terminal_event(
        ctx, "agent", _RaceTask(), sequence=3  # type: ignore[arg-type]
    )
    assert terminal.type == "response.completed"


@pytest.mark.asyncio
async def test_build_terminal_event_failed_when_run_task_raises() -> None:
    """A failed ``run_turn`` task surfaces as ``response.failed``."""

    async def _boom() -> None:
        raise ValueError("boom")

    ctx = TurnContext("resp_failed", asyncio.Queue(), asyncio.Event())
    run_task = asyncio.create_task(_boom())
    with contextlib.suppress(ValueError):
        await run_task
    terminal = await HarnessApp()._build_terminal_event(ctx, "agent", run_task, sequence=4)
    assert terminal.type == "response.failed"
    assert terminal.response.error is not None
    assert terminal.response.error.message == "boom"