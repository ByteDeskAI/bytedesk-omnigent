"""Edge-path coverage for :mod:`omnigent.runner.app` helpers.

Exercises small, testable units that are not reached by the larger
integration suites in ``test_app_sessions_native`` / ``test_runner_dispatch``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.cost_plan import AdvisorVerdict
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner.app import (
    ResolvedSpec,
    _AUTO_FORWARDER_TASKS,
    _SUBAGENT_DELIVERY_ALREADY_DELIVERED,
    _SUBAGENT_DELIVERY_DELIVERED,
    _SUBAGENT_DELIVERY_MISSING_PARENT_INBOX,
    _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY,
    _SUBAGENT_DELIVERY_UNTRACKED,
    _apply_advisor_to_body,
    _build_pi_native_args,
    _cancel_auto_forwarder_task,
    _client_safe_error_detail,
    _codex_session_workspace,
    _deliver_subagent_wake_post,
    _encode_sse_event,
    _evaluate_policy_via_omnigent,
    _format_subagent_wake_notice,
    _forward_harness_response,
    _get_runner_llm_client,
    _is_context_overflow_error,
    _merge_advisor_note,
    _normalize_turn_error,
    _pi_args_have_provider,
    _pi_args_have_session_control,
    _pi_native_launch_config,
    _pi_session_workspace,
    _publish_tmux_target_for_bridge,
    _required_runner_env,
    _resolve_forwarded_message_content,
    _resolved_spec_workdir,
    _response_body_preview,
    _response_failed_event,
    _session_agent_ids_ref,
    _session_inboxes_ref,
    _spec_builtin_tool_schemas,
    _spec_with_workdir_paths,
    _subagent_delivery_not_confirmed_response,
    _subagent_work_by_child,
    _subagent_work_by_parent,
    _terminal_lookup_miss_reason,
    _truncate_child_preview,
    _unwrap_resolved_spec,
    _wake_retry_sleep,
    _wrap_as_message_event,
    cancel_timer,
    get_session_agent_id,
    mark_subagent_work_started,
    mark_subagent_work_terminal,
    register_subagent_work,
    register_timer,
    unregister_subagent_work,
    unregister_subagent_work_for_session,
    unregister_timer,
)
from omnigent.runner.cost_advisor import AdvisorTurnResult
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.subagent_status import SubagentWorkStatus
from omnigent.spec.types import AgentSpec, LocalToolInfo
from omnigent.terminals import TerminalRegistry


# ── _client_safe_error_detail ─────────────────────────────────────────


def test_client_safe_error_detail_logs_and_redacts(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        detail = _client_safe_error_detail(
            RuntimeError("/secret/path exploded"),
            context="harness spawn",
        )
    assert detail == "Request failed on the runner; see runner logs for details."
    assert "/secret/path exploded" not in detail
    assert "/secret/path exploded" in caplog.text


# ── _get_runner_llm_client ────────────────────────────────────────────


def test_get_runner_llm_client_lazy_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.runner.app as app_mod

    sentinel = object()
    monkeypatch.setattr(app_mod, "_runner_llm_client", None)

    class _FakeClient:
        def __init__(self) -> None:
            pass

    monkeypatch.setattr("omnigent.llms.Client", _FakeClient)
    first = _get_runner_llm_client()
    second = _get_runner_llm_client()
    assert first is second
    assert isinstance(first, _FakeClient)
    monkeypatch.setattr(app_mod, "_runner_llm_client", None)


# ── _publish_tmux_target_for_bridge ───────────────────────────────────


def test_publish_tmux_target_noops_without_terminal_registry() -> None:
    registry = SessionResourceRegistry(terminal_registry=None)
    _publish_tmux_target_for_bridge(
        resource_registry=registry,
        session_id="conv_a",
        bridge_id="bridge_a",
        terminal_name="claude",
        session_key="main",
    )


def test_publish_tmux_target_noops_when_instance_missing_or_stopped(
    tmp_path: Path,
) -> None:
    terminal_registry = TerminalRegistry()
    stopped = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "sock",
        private_dir=tmp_path,
        running=False,
    )
    terminal_registry._by_conversation["conv_b"] = {("claude", "main"): stopped}
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    _publish_tmux_target_for_bridge(
        resource_registry=registry,
        session_id="conv_b",
        bridge_id="bridge_b",
        terminal_name="claude",
        session_key="main",
    )


def test_publish_tmux_target_writes_when_instance_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_registry = TerminalRegistry()
    running = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "sock",
        private_dir=tmp_path,
        running=True,
    )
    terminal_registry._by_conversation["conv_c"] = {("claude", "main"): running}
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)

    writes: list[tuple[Path, str, str]] = []

    def _fake_write(bridge_dir: Path, *, socket_path: Path, tmux_target: str) -> None:
        writes.append((bridge_dir, str(socket_path), tmux_target))

    monkeypatch.setattr(
        "omnigent.claude_native_bridge.bridge_dir_for_bridge_id",
        lambda bridge_id: tmp_path / bridge_id,
    )
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.write_tmux_target",
        _fake_write,
    )

    _publish_tmux_target_for_bridge(
        resource_registry=registry,
        session_id="conv_c",
        bridge_id="bridge_c",
        terminal_name="claude",
        session_key="main",
    )
    assert writes == [(tmp_path / "bridge_c", str(tmp_path / "sock"), "main")]


# ── _cancel_auto_forwarder_task timeout ───────────────────────────────


@pytest.mark.asyncio
async def test_cancel_auto_forwarder_task_logs_when_cancel_hangs(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "conv_fwd_timeout"

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never_finishes())
    _AUTO_FORWARDER_TASKS[session_id] = task

    async def _fake_wait(
        _tasks: set[asyncio.Task[Any]],
        *,
        timeout: float | None = None,
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        return set(), {task}

    monkeypatch.setattr(asyncio, "wait", _fake_wait)
    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            await _cancel_auto_forwarder_task(session_id)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        _AUTO_FORWARDER_TASKS.pop(session_id, None)

    assert "did not finish within" in caplog.text


# ── _terminal_lookup_miss_reason ────────────────────────────────────────


def test_terminal_lookup_miss_reason_branches(tmp_path: Path) -> None:
    missing_registry = SessionResourceRegistry(terminal_registry=None)
    assert _terminal_lookup_miss_reason(missing_registry, "conv_x", "terminal_x") == (
        "terminal_registry_missing"
    )

    empty_registry = SessionResourceRegistry(terminal_registry=TerminalRegistry())
    assert _terminal_lookup_miss_reason(empty_registry, "conv_y", "terminal_y") == (
        "session_has_no_registered_terminals"
    )

    terminal_registry = TerminalRegistry()
    stopped = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "sock",
        private_dir=tmp_path,
        running=False,
    )
    terminal_registry._by_conversation["conv_z"] = {("claude", "main"): stopped}
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    reason = _terminal_lookup_miss_reason(registry, "conv_z", "terminal_claude_main")
    assert reason.startswith("terminal_registered_but_not_running")

    running = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "sock2",
        private_dir=tmp_path,
        running=True,
    )
    terminal_registry._by_conversation["conv_w"] = {("codex", "main"): running}
    registry2 = SessionResourceRegistry(terminal_registry=terminal_registry)
    reason2 = _terminal_lookup_miss_reason(registry2, "conv_w", "terminal_codex_main")
    assert reason2.startswith("terminal_registered_but_liveness_probe_failed")

    reason3 = _terminal_lookup_miss_reason(registry2, "conv_w", "terminal_missing")
    assert "terminal_id_not_registered" in reason3


# ── workspace / env helpers ───────────────────────────────────────────


def test_required_runner_env_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    with pytest.raises(RuntimeError, match="OMNIGENT_RUNNER_WORKSPACE"):
        _required_runner_env("OMNIGENT_RUNNER_WORKSPACE")


def test_codex_and_pi_session_workspace_resolve_from_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "repo"))
    assert _codex_session_workspace(None) == (tmp_path / "repo").resolve()
    assert _pi_session_workspace("  ~/ignored  ") == Path("~/ignored").expanduser().resolve()


# ── Pi launch config / args ─────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int, payload: Any, *, json_raises: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


class _Client:
    def __init__(self, resp: _Resp | None = None, raise_exc: Exception | None = None) -> None:
        self._resp = resp
        self._raise_exc = raise_exc

    async def get(self, url: str, timeout: float | None = None) -> _Resp:
        del url, timeout
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._resp is not None
        return self._resp


@pytest.mark.asyncio
async def test_pi_native_launch_config_requires_server_client() -> None:
    with pytest.raises(RuntimeError, match="server_client is required"):
        await _pi_native_launch_config(session_id="conv_pi", server_client=None)


@pytest.mark.asyncio
async def test_pi_native_launch_config_transport_and_parse_errors() -> None:
    with pytest.raises(RuntimeError, match="Could not fetch Pi launch config"):
        await _pi_native_launch_config(
            session_id="conv_pi",
            server_client=_Client(raise_exc=httpx.ConnectError("down")),
        )
    with pytest.raises(RuntimeError, match="returned 500"):
        await _pi_native_launch_config(
            session_id="conv_pi",
            server_client=_Client(_Resp(500, None)),
        )
    with pytest.raises(RuntimeError, match="invalid JSON"):
        await _pi_native_launch_config(
            session_id="conv_pi",
            server_client=_Client(_Resp(200, None, json_raises=True)),
        )
    with pytest.raises(RuntimeError, match="not a JSON object"):
        await _pi_native_launch_config(
            session_id="conv_pi",
            server_client=_Client(_Resp(200, [])),
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("terminal_launch_args", "bad", "terminal_launch_args"),
        ("external_session_id", "", "external_session_id"),
        ("workspace", 5, "workspace"),
    ],
)
@pytest.mark.asyncio
async def test_pi_native_launch_config_invalid_fields(
    field: str,
    value: Any,
    match: str,
) -> None:
    with pytest.raises(RuntimeError, match=match):
        await _pi_native_launch_config(
            session_id="conv_pi",
            server_client=_Client(_Resp(200, {field: value})),
        )


@pytest.mark.asyncio
async def test_pi_native_launch_config_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:9000/")
    cfg = await _pi_native_launch_config(
        session_id="conv_pi_ok",
        server_client=_Client(
            _Resp(
                200,
                {
                    "workspace": str(tmp_path),
                    "terminal_launch_args": ["--continue"],
                    "external_session_id": "pi_sess_1",
                },
            )
        ),
    )
    assert cfg.server_url == "http://127.0.0.1:9000"
    assert cfg.terminal_launch_args == ["--continue"]
    assert cfg.external_session_id == "pi_sess_1"


def test_pi_args_helpers_and_build_args(tmp_path: Path) -> None:
    assert _pi_args_have_session_control(["--resume", "abc"])
    assert _pi_args_have_session_control(["--session-dir=/tmp"])
    assert not _pi_args_have_session_control(["--help"])

    assert _pi_args_have_provider(["--model", "gpt-4"])
    assert _pi_args_have_provider(["--api-key=secret"])
    assert not _pi_args_have_provider(["--extension", "/path"])

    args = _build_pi_native_args(
        terminal_launch_args=["--resume", "pi_ext"],
        extension_path=tmp_path / "ext.ts",
        session_dir=tmp_path / "sessions",
        external_session_id="pi_ext",
    )
    assert args[0:2] == ["--extension", str(tmp_path / "ext.ts")]
    assert "--session-dir" not in args
    assert args[-2:] == ["--resume", "pi_ext"]

    with_session = _build_pi_native_args(
        terminal_launch_args=None,
        extension_path=tmp_path / "ext.ts",
        session_dir=tmp_path / "sessions",
        external_session_id="pi_ext",
    )
    assert ["--session-dir", str(tmp_path / "sessions"), "--session", "pi_ext"] == with_session[2:6]


# ── SSE / response helpers ──────────────────────────────────────────────


def test_encode_sse_event_round_trip() -> None:
    payload = {"type": "heartbeat", "ts": 1}
    frame = _encode_sse_event(payload)
    assert frame == f"data: {json.dumps(payload)}\n\n".encode()


def test_forward_harness_response_empty_body_and_non_json() -> None:
    no_body = _forward_harness_response(httpx.Response(304, content=b"stale"))
    assert no_body.status_code == 304
    assert no_body.body == b""

    empty = _forward_harness_response(httpx.Response(200, content=b"", headers={}))
    assert empty.status_code == 200
    assert empty.body == b""

    raw = _forward_harness_response(
        httpx.Response(
            502,
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
    )
    assert raw.status_code == 502
    assert raw.body == b"not-json"


def test_response_body_preview_supports_fakes() -> None:
    class _FakeResp:
        content = b"hello bytes"

    assert _response_body_preview(_FakeResp()) == "hello bytes"
    assert _response_body_preview(SimpleNamespace(text="plain")) == "plain"
    assert _response_body_preview(SimpleNamespace(content="str-content")) == "str-content"
    assert _response_body_preview(object()) == ""


def test_response_failed_event_shape() -> None:
    frame = _response_failed_event({"code": "x", "message": "boom"})
    assert frame.startswith(b"event: response.failed")
    assert b'"type": "response.failed"' in frame


# ── policy evaluation proxy ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_policy_via_omnigent_success_and_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness_posts: list[dict[str, Any]] = []

    async def _server_post(url: str, **kwargs: Any) -> httpx.Response:
        del url
        return httpx.Response(
            200,
            json={"result": "POLICY_ACTION_DENY", "reason": "blocked", "data": {"k": 1}},
        )

    async def _harness_post(url: str, **kwargs: Any) -> httpx.Response:
        del url
        harness_posts.append(kwargs["json"])
        return httpx.Response(204)

    server_client = SimpleNamespace(post=AsyncMock(side_effect=_server_post))
    harness_client = SimpleNamespace(post=AsyncMock(side_effect=_harness_post))

    await _evaluate_policy_via_omnigent(
        server_client=server_client,  # type: ignore[arg-type]
        harness_client=harness_client,  # type: ignore[arg-type]
        conversation_id="conv_pol",
        evaluation_id="eval_1",
        phase="PHASE_LLM_REQUEST",
        data={"prompt": "hi"},
    )
    assert harness_posts[0]["action"] == "POLICY_ACTION_DENY"
    assert harness_posts[0]["reason"] == "blocked"
    assert harness_posts[0]["data"] == {"k": 1}


@pytest.mark.asyncio
async def test_evaluate_policy_via_omnigent_fail_open_on_transport_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server_client = SimpleNamespace(post=AsyncMock(side_effect=httpx.ConnectError("ap down")))
    harness_client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(204)))

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _evaluate_policy_via_omnigent(
            server_client=server_client,  # type: ignore[arg-type]
            harness_client=harness_client,  # type: ignore[arg-type]
            conversation_id="conv_pol3",
            evaluation_id="eval_3",
            phase="PHASE_TOOL_CALL",
            data={},
        )
    assert "defaulting to ALLOW" in caplog.text
    harness_client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_policy_via_omnigent_fail_open_and_delivery_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _server_post(url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        return httpx.Response(503)

    async def _harness_post(url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        raise httpx.ConnectError("harness down")

    server_client = SimpleNamespace(post=AsyncMock(side_effect=_server_post))
    harness_client = SimpleNamespace(post=AsyncMock(side_effect=_harness_post))

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _evaluate_policy_via_omnigent(
            server_client=server_client,  # type: ignore[arg-type]
            harness_client=harness_client,  # type: ignore[arg-type]
            conversation_id="conv_pol2",
            evaluation_id="eval_2",
            phase="PHASE_TOOL_CALL",
            data={},
        )
    assert "defaulting to ALLOW" in caplog.text
    assert "Failed to deliver policy verdict" in caplog.text


# ── advisor merge / wrap helpers ──────────────────────────────────────


def _verdict() -> AdvisorVerdict:
    return AdvisorVerdict(
        version=3,
        tier="cheap",
        model="gpt-4o-mini",
        applied=True,
        rationale="simple turn",
        turn_anchor="turn-1",
    )


def test_merge_advisor_note_all_content_shapes() -> None:
    note = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "[Cost advisor: cheap]"}],
    }
    assert _merge_advisor_note("plain", note) == [
        {"type": "input_text", "text": "plain"},
        *note["content"],
    ]
    merged = _merge_advisor_note(
        [{"type": "message", "role": "assistant", "content": "ignored"}],
        note,
    )
    assert merged[-1] is note
    merged_user = _merge_advisor_note(
        [{"type": "message", "role": "user", "content": "question"}],
        note,
    )
    assert merged_user[0]["content"][-1] == note["content"][0]
    blocks = _merge_advisor_note([{"type": "input_text", "text": "raw"}], note)
    assert blocks[-1] == note["content"][0]


def test_apply_advisor_to_body_and_wrap_as_message_event() -> None:
    body: dict[str, Any] = {"content": [{"type": "input_text", "text": "hi"}]}
    note = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "[note]"}],
    }
    result = AdvisorTurnResult(verdict=_verdict(), apply_model="gpt-4o", note_item=note)
    _apply_advisor_to_body(body, result)
    assert body["model_override"] == "gpt-4o"
    assert body["content"][-1]["text"] == "[note]"

    wrapped = _wrap_as_message_event({"model": "agent", "input": [{"text": "x"}]})
    assert wrapped["type"] == "message"
    assert wrapped["role"] == "user"
    assert wrapped["content"] == [{"text": "x"}]
    assert "input" not in wrapped


# ── context overflow detection ────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("context_length_exceeded: 150000 > 128000", (128000, 150000)),
        ("maximum context length is 12000 tokens", (12000, 12001)),
        ("context window exceeded at 9000", (9000, 9001)),
        ("context window oops", (128000, 128001)),
        ("totally unrelated", None),
    ],
)
def test_is_context_overflow_error(message: str, expected: tuple[int, int] | None) -> None:
    event = {"type": "response.failed", "error": {"message": message}}
    assert _is_context_overflow_error(event) == expected


# ── resolved spec / workdir helpers ───────────────────────────────────


def test_unwrap_and_workdir_helpers(tmp_path: Path) -> None:
    spec = AgentSpec(spec_version=1, name="agent")
    wrapped = ResolvedSpec(spec=spec, workdir=tmp_path)
    assert _unwrap_resolved_spec(wrapped) is spec
    assert _unwrap_resolved_spec(spec) is spec
    assert _resolved_spec_workdir(wrapped) == tmp_path
    assert _resolved_spec_workdir(spec) is None


def test_spec_with_workdir_paths_resolves_relative_tools(tmp_path: Path) -> None:
    spec = AgentSpec(
        spec_version=1,
        name="agent",
        local_tools=[
            LocalToolInfo(name="tool_a", path="tools/a.py", language="python"),
            LocalToolInfo(name="tool_b", path="/abs/b.py", language="python"),
        ],
    )
    resolved = _spec_with_workdir_paths(spec, tmp_path)
    assert resolved.local_tools[0].path == str((tmp_path / "tools/a.py").resolve())
    assert resolved.local_tools[1].path == "/abs/b.py"
    assert _spec_with_workdir_paths(spec, None) is spec
    assert _spec_with_workdir_paths(None, tmp_path) is None
    bare = AgentSpec(spec_version=1, name="bare")
    assert _spec_with_workdir_paths(bare, tmp_path) is bare


def test_spec_builtin_tool_schemas_returns_empty_on_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.tools.manager.ToolManager",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        assert _spec_builtin_tool_schemas(AgentSpec(spec_version=1, name="x"), None) == []


# ── forwarded file_id resolution ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_forwarded_message_content_inlines_files(tmp_path: Path) -> None:
    content = [
        {"type": "input_text", "text": "see image"},
        {"type": "input_image", "file_id": "file_img"},
        {"type": "input_file", "file_id": ""},
        {"type": "input_file", "file_id": "file_doc"},
    ]

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            media = (
                "image/png"
                if "/file_img/" in request.url.path
                else "application/octet-stream"
            )
            return httpx.Response(200, content=b"BYTES", headers={"content-type": media})
        if "/file_img" in request.url.path:
            return httpx.Response(200, json={"content_type": "image/png"}, request=request)
        return httpx.Response(200, json={"content_type": "application/pdf"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://srv")
    try:
        resolved = await _resolve_forwarded_message_content(
            content,
            session_id="conv_files",
            server_client=client,
        )
    finally:
        await client.aclose()

    assert resolved[0] == content[0]
    assert resolved[1]["image_url"].startswith("data:image/png;base64,")
    assert resolved[2]["file_id"] == ""
    assert resolved[3]["file_data"].startswith("data:application/pdf;base64,")


@pytest.mark.asyncio
async def test_resolve_forwarded_message_content_keeps_block_on_http_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://srv")
    block = {"type": "input_file", "file_id": "missing"}
    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            resolved = await _resolve_forwarded_message_content(
                [block],
                session_id="conv_miss",
                server_client=client,
            )
    finally:
        await client.aclose()
    assert resolved == [block]


# ── sub-agent registry edges ───────────────────────────────────────────


def _cleanup_subagent(child: str, parent: str | None = None) -> None:
    unregister_subagent_work(child)
    if parent is not None:
        _subagent_work_by_parent.pop(parent, None)


def test_register_subagent_work_replaces_prior_parent_index() -> None:
    child = "conv_child_reindex"
    first = register_subagent_work(
        parent_session_id="conv_parent_a",
        child_session_id=child,
        agent="worker",
        title="a",
    )
    second = register_subagent_work(
        parent_session_id="conv_parent_b",
        child_session_id=child,
        agent="worker",
        title="b",
    )
    try:
        assert first.work_id != second.work_id
        assert child not in _subagent_work_by_parent.get("conv_parent_a", set())
        assert child in _subagent_work_by_parent["conv_parent_b"]
    finally:
        _cleanup_subagent(child, "conv_parent_b")


def test_unregister_subagent_work_respects_work_id_guard() -> None:
    child = "conv_child_guard"
    entry = register_subagent_work(
        parent_session_id="conv_parent_guard",
        child_session_id=child,
        agent="worker",
        title="t",
    )
    try:
        unregister_subagent_work(child, work_id="wrong_id")
        assert _subagent_work_by_child[child] is entry
        unregister_subagent_work(child, work_id=entry.work_id, remember_drained_delivery=True)
        assert child not in _subagent_work_by_child
    finally:
        _subagent_work_by_parent.pop("conv_parent_guard", None)


def test_unregister_subagent_work_for_session_clears_parent_and_children() -> None:
    parent = "conv_parent_tree"
    child = "conv_child_tree"
    register_subagent_work(parent_session_id=parent, child_session_id=child, agent="w", title="t")
    try:
        unregister_subagent_work_for_session(parent)
        assert child not in _subagent_work_by_child
        assert parent not in _subagent_work_by_parent
    finally:
        _subagent_work_by_child.pop(child, None)
        _subagent_work_by_parent.pop(parent, None)


@pytest.mark.asyncio
async def test_mark_subagent_work_terminal_delivery_paths() -> None:
    parent = "conv_parent_inbox"
    child = "conv_child_inbox"
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _session_inboxes_ref[parent] = inbox
    register_subagent_work(parent_session_id=parent, child_session_id=child, agent="w", title="t")
    try:
        ack_missing = mark_subagent_work_terminal(
            "conv_unknown_child",
            status=SubagentWorkStatus.COMPLETED,
            output="x",
        )
        assert ack_missing.reason == _SUBAGENT_DELIVERY_UNTRACKED

        mark_subagent_work_started(child)

        ack = mark_subagent_work_terminal(
            child,
            status=SubagentWorkStatus.COMPLETED,
            output=None,
        )
        assert ack.delivered is True
        assert ack.reason == _SUBAGENT_DELIVERY_DELIVERED
        payload = await inbox.get()
        assert "no output" in payload["output"]

        ack_dup = mark_subagent_work_terminal(
            child,
            status=SubagentWorkStatus.COMPLETED,
            output="again",
        )
        assert ack_dup.reason == _SUBAGENT_DELIVERY_ALREADY_DELIVERED

        unregister_subagent_work(child, remember_drained_delivery=True)
        ack_drained = mark_subagent_work_terminal(
            child,
            status=SubagentWorkStatus.COMPLETED,
            output="x",
        )
        assert ack_drained.reason == _SUBAGENT_DELIVERY_ALREADY_DELIVERED
    finally:
        _cleanup_subagent(child, parent)
        _session_inboxes_ref.pop(parent, None)


def test_mark_subagent_work_terminal_missing_parent_inbox() -> None:
    parent = "conv_parent_no_inbox"
    child = "conv_child_no_inbox"
    register_subagent_work(parent_session_id=parent, child_session_id=child, agent="w", title="t")
    try:
        mark_subagent_work_started(child)
        ack = mark_subagent_work_terminal(
            child,
            status=SubagentWorkStatus.FAILED,
            output="err",
        )
        assert ack.reason == _SUBAGENT_DELIVERY_MISSING_PARENT_INBOX
    finally:
        _cleanup_subagent(child, parent)


def test_subagent_delivery_not_confirmed_response_matrix() -> None:
    from omnigent.runner.app import _SubagentDeliveryAck

    assert _subagent_delivery_not_confirmed_response(
        _SubagentDeliveryAck(entry=None, delivered=True, delivered_now=False, reason="x"),
        is_runner_known_subagent=True,
    ) is None
    resp = _subagent_delivery_not_confirmed_response(
        _SubagentDeliveryAck(
            entry=None,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_MISSING_WORK_ENTRY,
        ),
        is_runner_known_subagent=True,
    )
    assert resp is not None and resp.status_code == 503
    assert _subagent_delivery_not_confirmed_response(
        _SubagentDeliveryAck(
            entry=None,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_UNTRACKED,
        ),
        is_runner_known_subagent=False,
    ) is None


def test_format_subagent_wake_notice_pluralization() -> None:
    one = _format_subagent_wake_notice(agent="w", title="t", status="completed", pending=1)
    many = _format_subagent_wake_notice(agent="w", title="t", status="failed", pending=2)
    assert "1 result waiting" in one
    assert "2 results waiting" in many


# ── wake POST retry ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_subagent_wake_post_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        req = httpx.Request("POST", url)
        if calls["n"] == 1:
            return httpx.Response(503, request=req)
        return httpx.Response(204, request=req)

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = SimpleNamespace(post=AsyncMock(side_effect=_post))
    monkeypatch.setattr("omnigent.runner.app._wake_retry_sleep", _fake_sleep)
    assert await _deliver_subagent_wake_post(client, "conv_parent", "notice") is True  # type: ignore[arg-type]
    assert calls["n"] == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_deliver_subagent_wake_post_stops_on_permanent_4xx() -> None:
    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        return httpx.Response(400, request=httpx.Request("POST", url))

    client = SimpleNamespace(post=AsyncMock(side_effect=_post))
    assert await _deliver_subagent_wake_post(client, "conv_parent", "notice") is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_wake_retry_sleep_awaits() -> None:
    await _wake_retry_sleep(0)


# ── timer registry / session agent id ─────────────────────────────────


def test_timer_registry_and_session_agent_id() -> None:
    parent = "conv_timer"
    _session_agent_ids_ref[parent] = "ag_timer"

    class _FakeTask:
        cancelled = False

        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            self.cancelled = True
            return True

    task = _FakeTask()
    register_timer(parent, "timer_1", task)  # type: ignore[arg-type]
    assert cancel_timer(parent, "timer_missing") is False
    assert cancel_timer(parent, "timer_1") is True
    assert task.cancelled is True
    unregister_timer(parent, "timer_1")
    assert get_session_agent_id(parent) == "ag_timer"
    _session_agent_ids_ref.pop(parent, None)


# ── normalize turn error ────────────────────────────────────────────────


def test_normalize_turn_error_shapes() -> None:
    assert _normalize_turn_error({"message": "  boom  "}) == {
        "code": "runner_error",
        "message": "  boom  ",
    }
    assert _normalize_turn_error({"status": 502})["message"] == "turn failed (status 502)"
    assert _normalize_turn_error({"type": "custom", "message": ""}) == {
        "code": "custom",
        "message": "turn failed",
    }


def test_truncate_child_preview_short_path() -> None:
    assert _truncate_child_preview("ok") == "ok"