"""Batch-17 coverage for the last 16 runner.app statement gaps."""

from __future__ import annotations

import asyncio
import inspect
import contextlib
import shutil
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import app as runner_app_mod
from omnigent.runner import create_runner_app
from omnigent.runner.app import ResolvedSpec, _ContextWindowOverflow
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_runner_route_edges import _sse
from tests.runner.test_app_sessions_native import (
    _ADVISOR_TIERS_YAML,
    _FakeProcessManager,
    _LabelPatchRecordingServerClient,
    _ScriptedHarnessClient,
    _WakeRecordingServerClient,
    _runner_client,
)
from tests.runner.test_comment_relay import _StubResourceRegistry, _TOOL_RELAY_FILE
from tests.runner.test_native_subagent_harness_resolution import PARENT_AGENT_ID


def _walk_closure_objects(root: Any) -> list[Any]:
    """Depth-first walk of nested closure cell contents."""
    seen: set[int] = set()
    stack = [root]
    found: list[Any] = []
    while stack:
        obj = stack.pop()
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)
        found.append(obj)
        closure = getattr(obj, "__closure__", None)
        if closure:
            for cell in closure:
                stack.append(cell.cell_contents)
    return found


def _closure_named(app: FastAPI, name: str) -> Any:
    """Return the first nested closure function with the given ``__name__``."""
    for route in app.router.routes:
        endpoint = getattr(route, "endpoint", None)
        for obj in _walk_closure_objects(endpoint):
            if getattr(obj, "__name__", None) == name:
                return obj
    raise AssertionError(f"closure {name!r} not found on app")


def _wake_pending_set(app: FastAPI) -> set[str]:
    """Return the per-app ``_subagent_wake_pending`` debounce set."""
    scheduler = _closure_named(app, "_schedule_subagent_wake")
    for obj in _walk_closure_objects(scheduler):
        if isinstance(obj, set):
            return obj
    raise AssertionError("_subagent_wake_pending set not found")


def _session_spec_cache_dict(app: FastAPI) -> dict[str, Any]:
    """Return the per-app ``_session_spec_cache`` from summarize closure cells."""
    resolve_conn = _closure_named(app, "_resolve_summarize_connection")
    for cell in resolve_conn.__closure__ or ():
        if isinstance(cell.cell_contents, dict):
            return cell.cell_contents
    raise AssertionError("_session_spec_cache dict not found")


@pytest.mark.asyncio
async def test_session_snapshot_recheck_under_lock_returns_cached(
    tmp_path: Path,
) -> None:
    """Followers waiting on the snapshot lock return the cached snapshot (line 4874)."""
    conv = f"conv_snap_recheck_{uuid.uuid4().hex[:8]}"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fetch_count = 0
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            fetch_count += 1
            if fetch_count == 1:
                first_entered.set()
                await release.wait()
            return httpx.Response(
                200,
                json={"id": conv, "agent_id": "ag_snap", "workspace": str(workspace)},
            )
        return httpx.Response(200, json={})

    spec = AgentSpec(
        spec_version=1,
        name="snap-recheck",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        runner_workspace=workspace,
        spec_resolver=_resolver,
        server_client=server_client,
        resource_registry=_StubResourceRegistry(tmp_path),
    )
    session_snapshot = _closure_named(app, "_session_snapshot")
    try:
        leader = asyncio.create_task(session_snapshot(conv))
        await asyncio.wait_for(first_entered.wait(), timeout=5.0)
        followers = [asyncio.create_task(session_snapshot(conv)) for _ in range(4)]
        release.set()
        await asyncio.gather(leader, *followers)
    finally:
        await server_client.aclose()

    assert fetch_count == 1


@pytest.mark.asyncio
async def test_stream_disconnect_requeues_unsent_event_at_yield() -> None:
    """GeneratorExit during SSE yield re-queues the unsent event (lines 5702-5704)."""
    sid = f"conv_stream_requeue2_{uuid.uuid4().hex[:8]}"
    runner_app_mod._session_event_queues_ref.pop(sid, None)
    pending_event = {"type": "session.status", "status": "running", "id": "evt_requeue"}
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    stream_route = next(
        route
        for route in app.router.routes
        if getattr(route, "path", None) == "/v1/sessions/{session_id}/stream"
    )

    try:
        runner_app_mod._session_event_queues_ref[sid] = queue
        first = await stream_route.endpoint(session_id=sid)  # type: ignore[attr-defined]
        gen = first.body_iterator
        await gen.__anext__()
        queue.put_nowait(pending_event)
        consumer = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0.05)
        with pytest.raises(StopAsyncIteration):
            await gen.athrow(GeneratorExit())
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await consumer
        assert queue.qsize() == 1
        assert queue.get_nowait() == pending_event
    finally:
        runner_app_mod._session_event_queues_ref.pop(sid, None)


@pytest.mark.asyncio
async def test_cancel_active_turn_returns_false_when_no_live_task() -> None:
    """_cancel_active_turn is a no-op when the session has no running turn (line 7563)."""
    conv = f"conv_cancel_noop_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    cancel_active = _closure_named(app, "_cancel_active_turn")
    assert await cancel_active(conv) is False


@pytest.mark.asyncio
async def test_schedule_subagent_wake_skips_when_parent_inbox_missing() -> None:
    """Sub-agent wake scheduling is a no-op when the parent inbox vanished (line 7763)."""
    parent_id = f"conv_parent_no_inbox_{uuid.uuid4().hex[:8]}"
    child_id = f"conv_child_no_inbox_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=_WakeRecordingServerClient(parent_id),  # type: ignore[arg-type]
    )
    schedule_wake = _closure_named(app, "_schedule_subagent_wake")
    runner_app_mod.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="no-inbox",
    )
    entry = runner_app_mod.list_subagent_work(parent_id)[0]
    runner_app_mod._session_inboxes_ref.pop(parent_id, None)

    try:
        schedule_wake(entry)
    finally:
        runner_app_mod._session_inboxes_ref.pop(parent_id, None)
        runner_app_mod.unregister_subagent_work(child_id)


@pytest.mark.asyncio
async def test_rewake_parent_returns_when_inbox_has_no_work_entries() -> None:
    """Idle recovery clears a stuck flag even when work entries are gone (line 7818)."""
    parent_id = f"conv_parent_orphan_inbox_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    pending = _wake_pending_set(app)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    inbox.put_nowait({"handle_id": "orphan", "output": "stale"})
    runner_app_mod._session_inboxes_ref[parent_id] = inbox
    pending.add(parent_id)

    try:
        rewake = _closure_named(app, "_rewake_parent_if_inbox_stranded")
        rewake(parent_id)
        assert parent_id not in pending
    finally:
        runner_app_mod._session_inboxes_ref.pop(parent_id, None)
        pending.discard(parent_id)


@pytest.mark.asyncio
async def test_comment_relay_skips_when_relay_published_during_bridge_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second relay starter returns when the first publishes during bridge-id resolve (7959)."""
    session_id = f"conv_relay_bridge_race_{uuid.uuid4().hex[:8]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    bridge_calls = 0
    both_waiting = asyncio.Event()
    release_first_bridge = asyncio.Event()
    release_second_bridge = asyncio.Event()

    async def _gated_bridge_id(*_args: Any, **_kwargs: Any) -> str:
        nonlocal bridge_calls
        bridge_calls += 1
        if bridge_calls >= 2:
            both_waiting.set()
        if bridge_calls == 1:
            await release_first_bridge.wait()
        else:
            await release_second_bridge.wait()
        return session_id

    monkeypatch.setattr(
        runner_app_mod,
        "_claude_native_bridge_id_for_session",
        _gated_bridge_id,
    )
    monkeypatch.setattr("omnigent.claude_native_bridge.post_tools_changed", lambda *_a, **_k: None)

    app = create_runner_app(
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    ensure_relay = _closure_named(app, "_ensure_comment_relay_started")

    try:
        first = asyncio.create_task(ensure_relay(session_id, await_notify=False))
        second = asyncio.create_task(ensure_relay(session_id, await_notify=False))
        await asyncio.wait_for(both_waiting.wait(), timeout=5.0)
        release_first_bridge.set()
        for _ in range(50):
            if (bridge_dir / _TOOL_RELAY_FILE).exists():
                break
            await asyncio.sleep(0.02)
        assert (bridge_dir / _TOOL_RELAY_FILE).exists()
        release_second_bridge.set()
        await asyncio.gather(first, second)
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_comment_relay_skips_when_relay_published_during_spec_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second relay starter returns when the first publishes during spec resolve (8017)."""
    session_id = f"conv_relay_spec_skip_{uuid.uuid4().hex[:8]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    spec_entered = asyncio.Event()
    release_spec = asyncio.Event()

    async def _gated_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        spec_entered.set()
        await release_spec.wait()
        return AgentSpec(
            spec_version=1,
            name="relay-race",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    class _SnapshotServer(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> Any:
            del kwargs
            if f"/sessions/{session_id}" in url:
                return httpx.Response(
                    200,
                    json={"id": session_id, "agent_id": "ag_relay", "labels": {}},
                    request=httpx.Request("GET", url),
                )
            return await super().get(url)

    monkeypatch.setattr("omnigent.claude_native_bridge.post_tools_changed", lambda *_a, **_k: None)

    app = create_runner_app(
        spec_resolver=_gated_resolver,
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=_SnapshotServer(),  # type: ignore[arg-type]
    )
    ensure_relay = _closure_named(app, "_ensure_comment_relay_started")

    try:
        first = asyncio.create_task(
            ensure_relay(session_id, bridge_id=session_id, await_notify=False)
        )
        await asyncio.wait_for(spec_entered.wait(), timeout=5.0)
        second = asyncio.create_task(
            ensure_relay(session_id, bridge_id=session_id, await_notify=False)
        )
        await asyncio.sleep(0.02)
        release_spec.set()
        for _ in range(50):
            if (bridge_dir / _TOOL_RELAY_FILE).exists():
                break
            await asyncio.sleep(0.02)
        assert (bridge_dir / _TOOL_RELAY_FILE).exists()
        await asyncio.gather(first, second)
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_comment_relay_awaits_tools_changed_when_await_notify_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm-bridge relay start awaits post_tools_changed when await_notify=True (8160)."""
    session_id = f"conv_relay_await_notify_{uuid.uuid4().hex[:8]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    notify_started = threading.Event()
    release_notify = threading.Event()

    def _blocking_notify(*_args: Any, **_kwargs: Any) -> None:
        notify_started.set()
        release_notify.wait(timeout=5.0)

    monkeypatch.setattr(
        "omnigent.claude_native_bridge.post_tools_changed",
        _blocking_notify,
    )

    app = create_runner_app(
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    ensure_relay = _closure_named(app, "_ensure_comment_relay_started")

    try:
        task = asyncio.create_task(
            ensure_relay(
                session_id,
                explicit_bridge_dir=bridge_dir,
                await_notify=True,
            )
        )
        assert await asyncio.to_thread(notify_started.wait, 5.0)
        release_notify.set()
        await asyncio.wait_for(task, timeout=5.0)
        assert (bridge_dir / _TOOL_RELAY_FILE).exists()
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_stream_advisor_swaps_to_child_spec_when_parent_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream advisor planning swaps to the named worker child spec (line 8297)."""
    from omnigent.cost_plan import AdvisorVerdict
    from omnigent.runner import cost_advisor as cost_advisor_mod
    from omnigent.runtime.workflow import _find_spec_by_name as _real_find

    conv = f"conv_adv_child_swap_{uuid.uuid4().hex[:8]}"
    parent = AgentSpec(
        spec_version=1,
        name="advisor-orchestrator",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="worker",
                executor=ExecutorSpec(
                    type="omnigent",
                    config={
                        "harness": "claude-sdk",
                        "cost_optimize": _ADVISOR_TIERS_YAML,
                    },
                ),
            ),
        ],
    )
    judged_models: list[str] = []
    phase = {"create": True}

    def _find_spec_by_name(spec: AgentSpec, name: str) -> AgentSpec | None:
        if phase["create"]:
            return None
        return _real_find(spec, name)

    class _RecordingJudge:
        async def judge(self, *, query: str, turn_anchor: str) -> AdvisorVerdict | None:
            del query, turn_anchor
            judged_models.append("called")
            return AdvisorVerdict(
                tier="expensive",
                model="model-pricey",
                applied=False,
                rationale="child plan",
                turn_anchor="anchor",
            )

    monkeypatch.setattr(
        "omnigent.runtime.workflow._find_spec_by_name",
        _find_spec_by_name,
    )
    monkeypatch.setattr(
        cost_advisor_mod,
        "build_llm_judge",
        lambda **_kwargs: _RecordingJudge(),
    )

    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r_child_swap"}}),
            _sse({"type": "response.completed", "response": {"id": "r_child_swap"}}),
        ]
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return parent

    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_LabelPatchRecordingServerClient([]),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            create = await client.post(
                "/v1/sessions",
                json={
                    "session_id": conv,
                    "agent_id": PARENT_AGENT_ID,
                    "sub_agent_name": "worker",
                },
            )
            assert create.status_code == 201
            phase["create"] = False
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                params={"stream": "true"},
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": PARENT_AGENT_ID,
                    "model": "base",
                    "harness": "claude-sdk",
                    "content": [{"type": "input_text", "text": "plan"}],
                },
            )
            assert resp.status_code == 200
            _ = resp.text
        assert judged_models == ["called"]
    finally:
        runner_app_mod._session_histories_ref.pop(conv, None)


@pytest.mark.asyncio
async def test_run_turn_bg_reraises_setup_phase_context_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup-phase context-window overflow re-raises for the streaming handler (8338)."""
    from omnigent.runner import cost_advisor as cost_advisor_mod

    conv = f"conv_setup_overflow_{uuid.uuid4().hex[:8]}"
    observed: list[str] = []

    async def _overflow_advisor(**_kwargs: Any) -> None:
        raise _ContextWindowOverflow(128_000, 200_000)

    monkeypatch.setattr(cost_advisor_mod, "maybe_run_advisor", _overflow_advisor)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="overflow-agent",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": "openai-agents", "cost_optimize": "mode: optimize"},
            ),
        )

    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_of"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_of"}}),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    run_turn_bg = _closure_named(app, "_run_turn_bg")

    with pytest.raises(_ContextWindowOverflow):
        await run_turn_bg(
            {
                "type": "message",
                "role": "user",
                "agent_id": "ag_overflow",
                "model": "test",
                "harness": "openai-agents",
                "content": [{"type": "input_text", "text": "overflow"}],
            },
            conv,
        )
    observed.append("raised")
    assert observed == ["raised"]


def _reset_lazy_turn_spec_cells(resolve_fn: Any) -> None:
    """Reset per-turn lazy spec state via ``_resolve_turn_spec_lazy`` closure cells.

    ``proxy_stream`` resolves the turn spec once before posting to the harness.
    The dispatch path reuses the idempotent resolver; reset its closure cells so
    the second call at ``action_required`` re-enters resolution and can fail.
    """
    closure = resolve_fn.__closure__ or ()
    for cell in closure:
        value = cell.cell_contents
        if isinstance(value, bool):
            cell.cell_contents = False
        elif value is None or isinstance(value, AgentSpec):
            cell.cell_contents = None
        elif isinstance(value, dict) and value:
            # Agent-keyed cache populated on the first lazy resolve.
            if any(isinstance(v, AgentSpec) for v in value.values()):
                value.clear()


@pytest.mark.asyncio
async def test_stream_lazy_spec_failure_emits_response_failed_on_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lazy spec resolution failure during local dispatch emits response.failed (9323-9327)."""
    import omnigent.runner.app as app_module

    conv = f"conv_lazy_dispatch_fail_{uuid.uuid4().hex[:8]}"
    real_inject = app_module._inject_mcp_schemas

    def _reset_lazy_flag_after_builtin_inject(
        body: dict[str, Any], schemas: list[dict[str, Any]]
    ) -> None:
        real_inject(body, schemas)
        frame = inspect.currentframe()
        while frame is not None:
            if frame.f_code.co_name == "proxy_stream":
                resolve_fn = frame.f_locals.get("_resolve_turn_spec_lazy")
                if resolve_fn is not None:
                    _reset_lazy_turn_spec_cells(resolve_fn)
                break
            frame = frame.f_back

    monkeypatch.setattr(app_module, "_inject_mcp_schemas", _reset_lazy_flag_after_builtin_inject)

    spec = AgentSpec(
        spec_version=1,
        name="lazy-dispatch",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
        os_env=OSEnvSpec(
            type="caller_process",
            cwd="/tmp",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    resolve_calls = 0

    async def _counted_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 1:
            return spec
        raise RuntimeError("lazy spec failed at dispatch")

    chunks = [
        _sse({"type": "response.created", "response": {"id": "resp_lazy"}}),
        (
            'event: response.output_item.done\ndata: {"type":"response.output_item.done",'
            '"item":{"type":"function_call","status":"action_required",'
            '"name":"sys_os_read","call_id":"call_lazy","arguments":"{}"}}\n\n'
        ),
    ]
    hc = _ScriptedHarnessClient(chunks)

    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        spec_resolver=_counted_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv}/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_lazy",
                "model": "test",
                "harness": "openai-agents",
                "content": [{"type": "input_text", "text": "read"}],
            },
        )
    assert resp.status_code == 200
    assert resolve_calls == 2
    assert "event: response.failed" in resp.text
    assert "Failed to resolve the agent spec for this turn." in resp.text
    assert "RuntimeError" in resp.text


@pytest.mark.asyncio
async def test_summarize_returns_none_when_cached_spec_entry_has_null_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Summarize treats a cached ResolvedSpec with spec=None as missing auth (12248)."""
    captured: dict[str, Any] = {}

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="ok")])],
            )

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )

    conv = f"conv_null_spec_{uuid.uuid4().hex[:8]}"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    _session_spec_cache_dict(app)[conv] = ResolvedSpec(spec=None, workdir=tmp_path)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.post(
            "/v1/summarize",
            json={
                "session_id": conv,
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert captured["connection"] is None