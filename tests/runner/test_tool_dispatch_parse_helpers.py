"""Unit tests for small parsing/projection helpers in ``tool_dispatch.py``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner.tool_dispatch import (
    _execute_local_python_tool,
    _execute_spec_callable_tool,
    _is_spec_local_python_tool,
    _is_uc_function_tool,
    _parse_session_title,
    _ParsedTitle,
    _project_api_item,
    _resolve_spec_callable,
    _resolve_uc_profile,
    _text_from_api_content,
    _truncate_activity,
    should_dispatch_locally,
)
from omnigent.session_lifecycle import CLOSED_TITLE_INFIX
from omnigent.spec.types import LocalToolInfo, ToolRuntime
from omnigent.tools.builtins.spawn import _ACTIVITY_MAX_CHARS


def test_parse_session_title_splits_agent_and_instance() -> None:
    parsed = _parse_session_title("researcher:auth")
    assert parsed == _ParsedTitle(agent="researcher", title="auth")


def test_parse_session_title_parses_ui_prefixed_titles() -> None:
    parsed = _parse_session_title("ui:claude-native-ui:1")
    assert parsed == _ParsedTitle(agent="claude-native-ui", title="1")


def test_parse_session_title_strips_closed_marker_before_parse() -> None:
    raw = f"researcher:auth{CLOSED_TITLE_INFIX}conv_child"
    parsed = _parse_session_title(raw)
    assert parsed == _ParsedTitle(agent="researcher", title="auth")


def test_parse_session_title_returns_none_pair_for_top_level_rows() -> None:
    assert _parse_session_title(None) == _ParsedTitle(agent=None, title=None)
    assert _parse_session_title("standalone-title") == _ParsedTitle(agent=None, title=None)


def test_truncate_activity_bounds_long_text() -> None:
    short = "hello"
    assert _truncate_activity(short) == short
    long_text = "x" * (_ACTIVITY_MAX_CHARS + 10)
    truncated = _truncate_activity(long_text)
    assert truncated is not None
    assert truncated.endswith(" [truncated]")
    assert len(truncated) == _ACTIVITY_MAX_CHARS + len(" [truncated]")


def test_text_from_api_content_joins_output_blocks() -> None:
    content = [
        {"type": "output_text", "text": "first"},
        {"type": "output_text", "text": "second"},
        {"type": "image", "url": "ignored"},
    ]
    assert _text_from_api_content(content) == "first second"
    assert _text_from_api_content("not-a-list") == ""


def test_project_api_item_maps_function_call_shape() -> None:
    item = {
        "type": "function_call",
        "name": "sys_os_shell",
        "arguments": '{"command":"ls"}',
    }
    projected = _project_api_item(item)
    assert projected["type"] == "function_call"
    assert projected["tool"] == "sys_os_shell"
    assert projected["args"] == '{"command":"ls"}'


def test_project_api_item_maps_message_shape() -> None:
    item = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
    }
    projected = _project_api_item(item)
    assert projected["type"] == "message"
    assert projected["role"] == "assistant"
    assert projected["text"] == "hi"


def test_should_dispatch_locally_recognizes_builtin_tools() -> None:
    assert should_dispatch_locally("sys_os_shell") is True
    assert should_dispatch_locally("totally_unknown_tool_xyz") is False


def test_is_spec_local_python_tool_matches_declared_python_tools() -> None:
    spec = SimpleNamespace(
        local_tools=[
            SimpleNamespace(name="echo", language="python", path="json.dumps"),
            SimpleNamespace(name="js_tool", language="javascript", path="x.js"),
        ]
    )
    assert _is_spec_local_python_tool("echo", spec) is True
    assert _is_spec_local_python_tool("js_tool", spec) is False
    assert _is_spec_local_python_tool("missing", spec) is False
    assert _is_spec_local_python_tool("echo", None) is False


def test_resolve_spec_callable_imports_and_caches_callable() -> None:
    tool_dispatch._callable_cache.clear()
    spec = SimpleNamespace(local_tools=[SimpleNamespace(name="dumps", path="json.dumps")])
    resolved = _resolve_spec_callable("dumps", spec)
    assert callable(resolved)
    assert _resolve_spec_callable("dumps", spec) is resolved


def test_resolve_spec_callable_returns_error_for_missing_tool() -> None:
    assert "not in local dispatch table" in _resolve_spec_callable("nope", None)
    spec = SimpleNamespace(local_tools=[])
    assert "not in local dispatch table" in _resolve_spec_callable("nope", spec)


@pytest.mark.asyncio
async def test_execute_spec_callable_tool_invokes_sync_callable() -> None:
    tool_dispatch._callable_cache.clear()
    spec = SimpleNamespace(local_tools=[SimpleNamespace(name="dumps", path="json.dumps")])
    result = await _execute_spec_callable_tool("dumps", {"obj": {"a": 1}}, agent_spec=spec)
    assert '"a": 1' in result


@pytest.mark.asyncio
async def test_execute_local_python_tool_returns_error_without_agent_spec() -> None:
    result = await _execute_local_python_tool(
        "echo",
        "{}",
        agent_spec=None,
        conversation_id=None,
        task_id=None,
        agent_id=None,
        runner_workspace=None,
    )
    assert "no agent spec" in result


def test_is_uc_function_tool_matches_declared_runtime() -> None:
    spec = SimpleNamespace(
        local_tools=[
            LocalToolInfo(
                name="classify",
                path="catalog.schema.fn",
                language="sql",
                runtime=ToolRuntime.UC_FUNCTION,
            )
        ]
    )
    assert _is_uc_function_tool("classify", spec) is True
    assert _is_uc_function_tool("other", spec) is False
    assert _is_uc_function_tool("classify", None) is False


def test_resolve_uc_profile_prefers_executor_auth() -> None:
    auth = SimpleNamespace(profile="prod")
    executor = SimpleNamespace(auth=auth, profile="legacy", config={"profile": "cfg"})
    spec = SimpleNamespace(executor=executor)
    assert _resolve_uc_profile(spec) == "prod"


def test_resolve_uc_profile_falls_back_to_deprecated_and_config() -> None:
    executor_profile = SimpleNamespace(auth=None, profile="legacy", config={})
    assert _resolve_uc_profile(SimpleNamespace(executor=executor_profile)) == "legacy"

    executor_config = SimpleNamespace(auth=None, profile=None, config={"profile": "cfg"})
    assert _resolve_uc_profile(SimpleNamespace(executor=executor_config)) == "cfg"
    assert _resolve_uc_profile(None) is None
