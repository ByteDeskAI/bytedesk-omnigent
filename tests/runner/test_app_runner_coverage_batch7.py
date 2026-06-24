"""Batch-7 edge coverage for MCP exceptions, ingest paths, and helper branches."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    _auto_create_repl_terminal,
    _claude_native_session_wants_rebuild,
    _session_agent_ids_ref,
    _session_histories_ref,
)
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth
from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_runner_route_edges import _PaginatedServerClient, _sse
from tests.runner.test_app_sessions_native import (
    _BlockingHarnessClient,
    _FakeProcessManager,
    _ScriptedHarnessClient,
    _runner_client,
)


class _NoDataLineHarnessClient(_ScriptedHarnessClient):
    """Streams a keepalive frame without a data: line before completion."""

    def __init__(self) -> None:
        super().__init__(
            [
                _sse({"type": "response.created", "response": {"id": "resp_ping"}}),
                "event: ping\n\n",
                _sse({"type": "response.completed", "response": {"id": "resp_ping"}}),
            ]
        )


class _InjectionRejectHarnessClient(_BlockingHarnessClient):
    """Blocking harness whose mid-turn injection POST is rejected."""

    def __init__(self, sse_frames: list[str], gate: asyncio.Event) -> None:
        super().__init__(sse_frames, gate)
        self.injection_posts: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if "/events" in url and json.get("content"):
            self.injection_posts.append(json)

            class _Rejected:
                status_code = 422
                headers: dict[str, str] = {}
                content = b"rejected"

                @staticmethod
                def raise_for_status() -> None:
                    pass

            return _Rejected()
        return await super().post(url, json=json, timeout=timeout)


class _InjectionFailHarnessClient(_BlockingHarnessClient):
    """Blocking harness whose mid-turn injection POST raises a transport error."""

    def __init__(self, sse_frames: list[str], gate: asyncio.Event) -> None:
        super().__init__(sse_frames, gate)

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if "/events" in url and json.get("content"):
            raise httpx.ConnectError("injection transport down")
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_mcp_schemas_for_exception_logs_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A schemas_for exception is logged and the streaming turn still completes."""
    conv = "conv_mcp_exc"
    spec = AgentSpec(
        spec_version=1,
        name="mcp-exc-agent",
        mcp_servers=[{"name": "srv", "url": "http://mcp.example"}],
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_exc"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_exc"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]
    caplog.set_level(logging.ERROR, logger="omnigent.runner.app")

    class _ExplodingProxy:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def schemas_for(self, _spec: AgentSpec) -> McpSchemasResult:
            raise RuntimeError("schemas_for boom")

    monkeypatch.setattr("omnigent.runner.app.ProxyMcpManager", _ExplodingProxy)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_mcp_exc",
                "model": "test",
                "harness": "openai-agents",
                "has_mcp_servers": True,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 200
        async for _ in resp.aiter_text():
            pass

    assert "runner mcp_manager.schemas_for failed" in caplog.text


@pytest.mark.asyncio
async def test_stream_sse_frame_without_data_line_hits_no_data_branch() -> None:
    """SSE frames without a data: line set event=None (then relay crashes)."""
    conv = "conv_no_data_sse"
    spec = AgentSpec(spec_version=1, name="no-data-sse")
    hc = _NoDataLineHarnessClient()
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'get'"):
            await client.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_nd",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )


@pytest.mark.asyncio
async def test_summarize_provider_resolution_exception_returns_none_connection(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider resolution exceptions are logged and yield None connection params."""
    spec = AgentSpec(
        spec_version=1,
        name="provider-exc-agent",
        executor=ExecutorSpec(
            type="omnigent",
            model="gpt-4o-mini",
            auth=ProviderAuth(name="broken-provider"),
        ),
    )
    captured: dict[str, Any] = {}

    class _FakeResponses:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            captured["connection"] = kwargs.get("connection_params")
            return SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(text="ok")])],
            )

    def _boom_config() -> dict[str, Any]:
        raise OSError("config unreadable")

    monkeypatch.setattr(
        "omnigent.runner.app._get_runner_llm_client",
        lambda: SimpleNamespace(responses=_FakeResponses()),
    )
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", _boom_config)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv = "conv_provider_exc"

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag"})
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
    assert "failed to resolve provider 'broken-provider'" in caplog.text


@pytest.mark.asyncio
async def test_streaming_message_non_object_body_returns_400() -> None:
    """Streaming ingest rejects a JSON body that is not an object."""
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_bad_body/events?stream=true",
            content=b'"just a string"',
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    assert "JSON object" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_mid_turn_injection_rejected_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected mid-turn injection forward logs a warning but still buffers."""
    gate = asyncio.Event()
    spec = AgentSpec(spec_version=1, name="inj-reject")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_inj"}}),
        _sse({"type": "response.output_text.delta", "delta": "working"}),
        _sse({"type": "response.completed", "response": {"id": "resp_inj"}}),
    ]
    hc = _InjectionRejectHarnessClient(sse_frames, gate)
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await client.post(
                "/v1/sessions",
                json={"session_id": "conv_inj_reject", "agent_id": "ag_1"},
            )

            async def _first_turn() -> None:
                resp = await client.post(
                    "/v1/sessions/conv_inj_reject/events?stream=true",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "test",
                        "harness": "openai-agents",
                        "content": [{"type": "input_text", "text": "first"}],
                    },
                )
                async for _ in resp.aiter_text():
                    pass

            turn_task = asyncio.create_task(_first_turn())
            await asyncio.sleep(0.05)

            resp2 = await client.post(
                "/v1/sessions/conv_inj_reject/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "second"}],
                },
            )
            assert resp2.status_code == 202
            assert resp2.json()["status"] == "buffered"
            assert hc.injection_posts

            gate.set()
            await asyncio.wait_for(turn_task, timeout=5.0)

    assert "mid-turn injection forward rejected" in caplog.text


@pytest.mark.asyncio
async def test_mid_turn_injection_forward_failure_is_best_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transport errors during mid-turn injection are logged at debug and ignored."""
    gate = asyncio.Event()
    spec = AgentSpec(spec_version=1, name="inj-fail")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_if"}}),
        _sse({"type": "response.output_text.delta", "delta": "working"}),
        _sse({"type": "response.completed", "response": {"id": "resp_if"}}),
    ]
    hc = _InjectionFailHarnessClient(sse_frames, gate)
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await client.post(
                "/v1/sessions",
                json={"session_id": "conv_inj_fail", "agent_id": "ag_1"},
            )

            async def _first_turn() -> None:
                resp = await client.post(
                    "/v1/sessions/conv_inj_fail/events?stream=true",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "test",
                        "harness": "openai-agents",
                        "content": [{"type": "input_text", "text": "first"}],
                    },
                )
                async for _ in resp.aiter_text():
                    pass

            turn_task = asyncio.create_task(_first_turn())
            await asyncio.sleep(0.05)

            resp2 = await client.post(
                "/v1/sessions/conv_inj_fail/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "second"}],
                },
            )
            assert resp2.status_code == 202
            gate.set()
            await asyncio.wait_for(turn_task, timeout=5.0)

    assert "mid-turn injection forward failed for conv_inj_fail" in caplog.text


@pytest.mark.asyncio
async def test_agent_switch_releases_harness_between_turns() -> None:
    """A different agent_id on a later turn releases the cached harness client."""
    conv = "conv_agent_switch"
    spec = AgentSpec(spec_version=1, name="switch-agent")
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_sw1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_sw1"}}),
            _sse({"type": "response.created", "response": {"id": "resp_sw2"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_sw2"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": conv, "agent_id": "ag_old"})
        for agent_id in ("ag_old", "ag_new"):
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": agent_id,
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": f"from {agent_id}"}],
                },
            )
            assert resp.status_code == 202
            for _ in range(200):
                if len(hc.posted_bodies) >= (1 if agent_id == "ag_old" else 2):
                    break
                await asyncio.sleep(0.01)

    assert conv in pm.released
    assert _session_agent_ids_ref.get(conv) == "ag_new"


@pytest.mark.asyncio
async def test_run_turn_bg_spec_resolution_failure_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec resolver failures during _run_turn_bg are logged and non-fatal."""
    conv = "conv_spec_fail"
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_sf"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_sf"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        raise RuntimeError("resolver unavailable")

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_fail",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "go"}],
                },
            )
            assert resp.status_code == 202
            for _ in range(200):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)

    assert "Spec resolution failed for conv_spec_fail" in caplog.text


@pytest.mark.asyncio
async def test_run_turn_bg_loads_history_when_cache_empty() -> None:
    """Fire-and-forget turns load server history when the in-memory cache is cold."""
    conv = "conv_hist_load"
    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "prior"}],
        }
    ]
    spec = AgentSpec(spec_version=1, name="hist-agent")
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_hl"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_hl"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PaginatedServerClient(history),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_hist",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "new"}],
                },
            )
            assert resp.status_code == 202
            for _ in range(200):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.pop(conv, None)

    assert hc.posted_bodies
    content = hc.posted_bodies[0].get("content", [])
    assert any(
        isinstance(block, dict) and block.get("type") == "message"
        for block in content
    )


@pytest.mark.asyncio
async def test_run_turn_bg_cold_cache_wraps_inbound_message_in_history() -> None:
    """A cold-cache turn seeds history with the inbound message before streaming."""
    conv = f"conv_empty_hist_{uuid.uuid4().hex[:12]}"
    spec = AgentSpec(spec_version=1, name="empty-hist-agent")
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_eh"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_eh"}}),
        ]
    )
    pm = _FakeProcessManager(hc)  # type: ignore[arg-type]

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_PaginatedServerClient([]),  # type: ignore[arg-type]
    )

    inbound_content = [{"type": "input_text", "text": "only this turn"}]
    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_empty",
                    "model": "test",
                    "harness": "openai-agents",
                    "content": inbound_content,
                },
            )
            assert resp.status_code == 202
            for _ in range(200):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)
    finally:
        _session_histories_ref.pop(conv, None)

    assert hc.posted_bodies[0]["content"] == [
        {
            "type": "message",
            "role": "user",
            "content": inbound_content,
        }
    ]


@pytest.mark.asyncio
async def test_auto_create_repl_terminal_label_patch_failure_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed UI label PATCH is logged but the REPL terminal still launches."""
    session_id = "conv_repl_patch_fail"
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")

    class _FakeResourceRegistry:
        async def launch_auxiliary_terminal(self, **_kwargs: Any) -> SessionResourceView:
            return SessionResourceView(
                id="terminal_tui_main",
                type="terminal",
                session_id=session_id,
                name="tui",
            )

    class _FailingPatchClient:
        async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            raise httpx.ConnectError("ap unreachable")

    published: list[dict[str, Any]] = []

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        terminal_view = await _auto_create_repl_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: published.append(event),
            server_client=_FailingPatchClient(),  # type: ignore[arg-type]
        )

    assert terminal_view.id == "terminal_tui_main"
    assert published[0]["type"] == "session.resource.created"
    assert "Could not stamp" in caplog.text


@pytest.mark.asyncio
async def test_claude_native_session_wants_rebuild_without_server_client() -> None:
    """Missing server client short-circuits rebuild detection."""
    assert await _claude_native_session_wants_rebuild(None, "conv_none") is False


@pytest.mark.asyncio
async def test_claude_native_session_wants_rebuild_on_http_error() -> None:
    """HTTP errors while fetching the session snapshot return False."""

    class _FailingGetClient:
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            raise httpx.ConnectError("ap down")

    assert (
        await _claude_native_session_wants_rebuild(
            _FailingGetClient(),  # type: ignore[arg-type]
            "conv_http_err",
        )
        is False
    )


@pytest.mark.asyncio
async def test_claude_native_session_wants_rebuild_on_non_200_status() -> None:
    """Non-200 session snapshots are treated as no pending rebuild."""

    class _NotFoundClient:
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            return httpx.Response(404, json={"error": "not_found"})

    assert (
        await _claude_native_session_wants_rebuild(
            _NotFoundClient(),  # type: ignore[arg-type]
            "conv_missing",
        )
        is False
    )


@pytest.mark.asyncio
async def test_claude_native_session_wants_rebuild_when_external_session_set() -> None:
    """An existing external_session_id means this is a normal resume."""

    class _ResumeClient:
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            return httpx.Response(
                200,
                json={"external_session_id": "claude-abc", "labels": {}},
            )

    assert (
        await _claude_native_session_wants_rebuild(
            _ResumeClient(),  # type: ignore[arg-type]
            "conv_resume",
        )
        is False
    )


@pytest.mark.asyncio
async def test_claude_native_session_wants_rebuild_when_carry_history_label_set() -> None:
    """Carry-history without external_session_id signals a pending rebuild."""

    class _RebuildClient:
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            return httpx.Response(
                200,
                json={"labels": {FORK_CARRY_HISTORY_LABEL_KEY: "1"}},
            )

    assert (
        await _claude_native_session_wants_rebuild(
            _RebuildClient(),  # type: ignore[arg-type]
            "conv_rebuild",
        )
        is True
    )


@pytest.mark.asyncio
async def test_terminal_launch_missing_fields_returns_400() -> None:
    """POST /resources/terminals requires terminal and session_key."""
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_term_bad/resources/terminals",
            json={"terminal": "claude"},
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"