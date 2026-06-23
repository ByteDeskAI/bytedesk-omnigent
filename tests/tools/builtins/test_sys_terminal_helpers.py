"""
Unit tests for :mod:`omnigent.tools.builtins.sys_terminal` helpers and error paths.

These tests do not require tmux — integration coverage lives in
``test_sys_terminal.py``. Focus: validation, cwd synthesis, schema
identity, and structured error envelopes.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance
from omnigent.spec.types import AgentSpec
from omnigent.terminals.registry import TerminalRegistry
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.sys_terminal import (
    SysTerminalCloseTool,
    SysTerminalLaunchTool,
    SysTerminalListTool,
    SysTerminalReadTool,
    SysTerminalSendTool,
    _check_overrides,
    _describe_entry,
    _has_meaningful_cwd,
    _materialize_terminal_spec_for_launch,
    _parse_arguments,
    _resolve_running_instance,
    _synthesize_parent_os_env,
    _validate_session_required_args,
)


def _ctx(tmp_path: Path, *, conversation_id: str | None = "conv_test") -> ToolContext:
    return ToolContext(
        task_id="task_test",
        agent_id="agent_test",
        workspace=tmp_path,
        conversation_id=conversation_id,
    )


def _bash_spec(**kwargs: Any) -> TerminalEnvSpec:
    return TerminalEnvSpec(command="bash", **kwargs)


# ── cwd helpers ──────────────────────────────────────────────


def test_has_meaningful_cwd_treats_placeholders_as_absent() -> None:
    assert _has_meaningful_cwd(None) is False
    assert _has_meaningful_cwd("") is False
    assert _has_meaningful_cwd(".") is False
    assert _has_meaningful_cwd("./") is False
    assert _has_meaningful_cwd("/tmp/workspace") is True


def test_materialize_terminal_spec_noop_when_resolved_cwd_missing() -> None:
    terminal_spec = _bash_spec(os_env=OSEnvSpec(type="caller_process", cwd="."))
    assert _materialize_terminal_spec_for_launch(terminal_spec, None) is terminal_spec


def test_materialize_terminal_spec_leaves_explicit_terminal_cwd() -> None:
    terminal_spec = _bash_spec(os_env=OSEnvSpec(type="caller_process", cwd="/fixed"))
    assert _materialize_terminal_spec_for_launch(terminal_spec, "/ignored") is terminal_spec


def test_materialize_terminal_spec_leaves_inherit_os_env_unchanged(tmp_path: Path) -> None:
    terminal_spec = _bash_spec(os_env="inherit")
    result = _materialize_terminal_spec_for_launch(terminal_spec, str(tmp_path))
    assert result is terminal_spec


def test_materialize_terminal_spec_clones_placeholder_terminal_cwd(tmp_path: Path) -> None:
    terminal_spec = _bash_spec(
        os_env=OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none"))
    )
    result = _materialize_terminal_spec_for_launch(terminal_spec, str(tmp_path / "ws"))
    assert result is not terminal_spec
    assert isinstance(result.os_env, OSEnvSpec)
    assert result.os_env.cwd == str(tmp_path / "ws")


def test_synthesize_parent_os_env_branches() -> None:
    explicit = OSEnvSpec(type="caller_process", cwd="/explicit")
    assert _synthesize_parent_os_env(explicit, None) is explicit
    assert _synthesize_parent_os_env(None, "/ws").cwd == "/ws"
    placeholder = OSEnvSpec(type="caller_process", cwd=".")
    synthesized = _synthesize_parent_os_env(placeholder, "/resolved")
    assert synthesized.cwd == "/resolved"
    assert _synthesize_parent_os_env(explicit, "/ignored") is explicit


# ── argument parsing / validation ─────────────────────────────


def test_parse_arguments_handles_empty_malformed_and_non_object() -> None:
    assert _parse_arguments("") == {}
    bad = _parse_arguments("{not json")
    assert "error" in bad
    non_obj = _parse_arguments(json.dumps(["array"]))
    assert non_obj == {"error": "arguments must be a JSON object"}


def test_validate_session_required_args_rejects_empty_fields() -> None:
    assert _validate_session_required_args({}) == {
        "error": "requires a non-empty 'terminal' string",
    }
    assert _validate_session_required_args({"terminal": "bash"}) == {
        "error": "requires a non-empty 'session' string",
    }
    assert _validate_session_required_args({"terminal": "bash", "session": "s1"}) is None


def test_check_overrides_rejects_invalid_sandbox() -> None:
    spec = _bash_spec(allow_sandbox_override=True)
    result = _check_overrides("bash", spec, None, "docker")
    assert result is not None
    assert "invalid sandbox override" in json.loads(result)["error"]


def test_resolve_running_instance_error_envelopes(registry: TerminalRegistry, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    missing_conv = _resolve_running_instance(
        registry, "{}", _ctx(tmp_path, conversation_id=None), "sys_terminal_send"
    )
    assert "conversation_id" in json.loads(missing_conv)["error"]

    bad_json = _resolve_running_instance(registry, "{bad", ctx, "sys_terminal_send")
    assert "error" in json.loads(bad_json)

    missing_session = _resolve_running_instance(
        registry, json.dumps({"terminal": "bash"}), ctx, "sys_terminal_send"
    )
    assert "session" in json.loads(missing_session)["error"]


# ── tool identity / schema ──────────────────────────────────


@pytest.mark.parametrize(
    ("tool_cls", "expected_name"),
    [
        (SysTerminalLaunchTool, "sys_terminal_launch"),
        (SysTerminalSendTool, "sys_terminal_send"),
        (SysTerminalReadTool, "sys_terminal_read"),
        (SysTerminalListTool, "sys_terminal_list"),
        (SysTerminalCloseTool, "sys_terminal_close"),
    ],
)
def test_terminal_tool_names_and_schemas(
    tool_cls: type,
    expected_name: str,
    registry: TerminalRegistry,
    tmp_path: Path,
) -> None:
    if tool_cls is SysTerminalLaunchTool:
        tool = tool_cls(
            spec=AgentSpec(spec_version=1, name="t", terminals={"bash": _bash_spec()}),
            registry=registry,
        )
    else:
        tool = tool_cls(registry=registry)
    assert tool_cls.name() == expected_name
    assert len(tool_cls.description()) > 0
    schema = tool.get_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == expected_name


# ── launch error paths (no tmux) ──────────────────────────────


def test_launch_validate_rejects_missing_terminal(registry: TerminalRegistry, tmp_path: Path) -> None:
    spec = AgentSpec(spec_version=1, name="t", terminals={"bash": _bash_spec()})
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)
    result = tool._validate_launch_args(json.dumps({"terminal": "", "session": "s1"}))
    assert isinstance(result, str)
    assert "terminal" in json.loads(result)["error"]


def test_launch_validate_propagates_malformed_json(registry: TerminalRegistry) -> None:
    spec = AgentSpec(spec_version=1, name="t", terminals={"bash": _bash_spec()})
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)
    result = tool._validate_launch_args("{not-json")
    assert isinstance(result, str)
    assert "malformed arguments JSON" in json.loads(result)["error"]


def test_launch_perform_launch_maps_exceptions_to_json(
    registry: TerminalRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = AgentSpec(spec_version=1, name="t", terminals={"bash": _bash_spec()})
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)

    async def _boom(*_args: object, **_kwargs: object) -> TerminalInstance:
        raise RuntimeError("tmux missing")

    monkeypatch.setattr(registry, "launch", _boom)
    from omnigent.tools.builtins.sys_terminal import _ValidatedLaunchArgs

    validated = _ValidatedLaunchArgs(
        terminal_name="bash",
        session_key="s1",
        terminal_spec=_bash_spec(),
        cwd_override=None,
        sandbox_override=None,
    )
    result = tool._perform_launch("conv_test", validated, _bash_spec(), None)
    assert isinstance(result, str)
    assert "launch failed" in json.loads(result)["error"]


def test_spawn_and_format_propagates_perform_launch_error(
    registry: TerminalRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = AgentSpec(spec_version=1, name="t", terminals={"bash": _bash_spec()})
    tool = SysTerminalLaunchTool(spec=spec, registry=registry)
    monkeypatch.setattr(tool, "_perform_launch", lambda *_a, **_k: json.dumps({"error": "nope"}))
    from omnigent.tools.builtins.sys_terminal import _ValidatedLaunchArgs

    validated = _ValidatedLaunchArgs(
        terminal_name="bash",
        session_key="s1",
        terminal_spec=_bash_spec(),
        cwd_override=None,
        sandbox_override=None,
    )
    raw = tool._spawn_and_format(_ctx(tmp_path), validated, _bash_spec(), None, False)
    assert json.loads(raw) == {"error": "nope"}


# ── send / read / close with mocked instances ───────────────


@pytest.fixture
def registry() -> TerminalRegistry:
    return TerminalRegistry()


def _running_instance(registry: TerminalRegistry, ctx: ToolContext) -> TerminalInstance:
    instance = MagicMock(spec=TerminalInstance)
    instance.running = True
    instance.command = "bash"
    instance.os_env = None
    instance.socket_path = Path("/tmp/mock.sock")
    instance.send = AsyncMock(return_value={"status": "sent"})
    instance.read = AsyncMock(return_value={"screen": "ok"})
    assert ctx.conversation_id is not None
    lock_key = (ctx.conversation_id, "bash", "s1")
    with registry._lock:
        registry._by_conversation.setdefault(ctx.conversation_id, {})[("bash", "s1")] = instance
        registry._instance_locks[lock_key] = threading.Lock()
    return instance


def test_send_rejects_non_string_text_and_keys(registry: TerminalRegistry, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _running_instance(registry, ctx)
    tool = SysTerminalSendTool(registry=registry)

    bad_text = json.loads(
        tool.invoke(
            json.dumps({"terminal": "bash", "session": "s1", "text": 42}),
            ctx,
        )
    )
    assert bad_text["error"] == "'text' must be a string if provided"

    bad_keys = json.loads(
        tool.invoke(
            json.dumps({"terminal": "bash", "session": "s1", "keys": ["Enter"]}),
            ctx,
        )
    )
    assert bad_keys["error"] == "'keys' must be a string if provided"


def test_send_maps_instance_failures(registry: TerminalRegistry, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    instance = _running_instance(registry, ctx)

    async def _fail(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise OSError("stale socket")

    instance.send = _fail
    tool = SysTerminalSendTool(registry=registry)
    result = json.loads(
        tool.invoke(json.dumps({"terminal": "bash", "session": "s1", "text": "x"}), ctx)
    )
    assert "send failed" in result["error"]


def test_read_rejects_negative_scrollback(registry: TerminalRegistry, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _running_instance(registry, ctx)
    tool = SysTerminalReadTool(registry=registry)
    result = json.loads(
        tool.invoke(
            json.dumps({"terminal": "bash", "session": "s1", "scrollback": -1}),
            ctx,
        )
    )
    assert result["error"] == "'scrollback' must be a non-negative integer"


def test_read_maps_instance_failures(registry: TerminalRegistry, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    instance = _running_instance(registry, ctx)

    async def _fail(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise RuntimeError("capture failed")

    instance.read = _fail
    tool = SysTerminalReadTool(registry=registry)
    result = json.loads(tool.invoke(json.dumps({"terminal": "bash", "session": "s1"}), ctx))
    assert "read failed" in result["error"]


def test_list_requires_conversation_id(registry: TerminalRegistry, tmp_path: Path) -> None:
    tool = SysTerminalListTool(registry=registry)
    result = json.loads(tool.invoke("{}", _ctx(tmp_path, conversation_id=None)))
    assert "conversation_id" in result["error"]


def test_list_describes_registered_entries(registry: TerminalRegistry, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    instance = _running_instance(registry, ctx)
    tool = SysTerminalListTool(registry=registry)
    payload = json.loads(tool.invoke("{}", ctx))
    assert payload == [
        _describe_entry("bash", "s1", instance),
    ]


def test_close_validation_and_failure_paths(registry: TerminalRegistry, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    tool = SysTerminalCloseTool(registry=registry)

    missing_conv = json.loads(
        tool.invoke(json.dumps({"terminal": "bash", "session": "s1"}), _ctx(tmp_path, conversation_id=None))
    )
    assert "conversation_id" in missing_conv["error"]

    bad_json = json.loads(tool.invoke("{bad", ctx))
    assert "error" in bad_json

    missing_session = json.loads(tool.invoke(json.dumps({"terminal": "bash"}), ctx))
    assert "session" in missing_session["error"]


def test_close_maps_registry_failures(registry: TerminalRegistry, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _running_instance(registry, ctx)
    tool = SysTerminalCloseTool(registry=registry)

    async def _boom(*_args: object, **_kwargs: object) -> bool:
        raise OSError("tmux gone")

    registry.close = _boom  # type: ignore[method-assign]
    result = json.loads(tool.invoke(json.dumps({"terminal": "bash", "session": "s1"}), ctx))
    assert "close failed" in result["error"]