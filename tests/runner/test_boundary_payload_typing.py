"""Typing-contract tests for the runner<->server boundary payloads (BDP-2366).

These pin the structural payload contracts the sweep-2 boundary TypedDicts
name, and prove the ``sys_session_send`` boundary keeps its loud-vs-soft error
asymmetry after the annotations landed:

- a representative ``action_required`` SSE event conforms to its TypedDict and
  the runner accessors read it;
- a representative sub-agent inbox payload conforms to ``SubagentInboxPayload``;
- ``sys_session_send`` arg parsing fails **loud** on a malformed ``model``
  (raises ``ValueError``) but fails **soft** on a malformed message (``None``).

The TypedDicts are additionally pinned statically by ``mypy`` over the runner
modules; these runtime checks guard the wire shape + the error asymmetry.
"""

from __future__ import annotations

import pytest

from omnigent.runner.tool_dispatch import (
    ActionRequiredEvent,
    SubagentInboxPayload,
    SubagentSendArgs,
    _subagent_message_from_args,
    _subagent_model_from_args,
    get_arguments,
    get_call_id,
    get_tool_name,
    is_action_required,
)


def test_action_required_event_conforms_and_reads() -> None:
    """A representative action_required SSE event conforms + reads back."""
    event: ActionRequiredEvent = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "status": "action_required",
            "name": "sys_os_shell",
            "call_id": "call_abc123",
            "arguments": '{"command": "ls"}',
        },
    }
    assert is_action_required(event) is True
    assert get_tool_name(event) == "sys_os_shell"
    assert get_call_id(event) == "call_abc123"
    assert get_arguments(event) == '{"command": "ls"}'


def test_non_output_event_is_not_action_required() -> None:
    """An event with no item is not action_required (optional keys hold)."""
    event: ActionRequiredEvent = {"type": "response.created"}
    assert is_action_required(event) is False


def test_subagent_inbox_payload_conforms() -> None:
    """A representative terminal inbox payload conforms to the TypedDict."""
    payload: SubagentInboxPayload = {
        "type": "sub_agent",
        "work_id": "work_1",
        "task_id": "conv_child",
        "handle_id": "conv_child",
        "conversation_id": "conv_child",
        "tool_name": "researcher",
        "agent": "researcher",
        "title": "researcher:auth",
        "status": "completed",
        "output": "done",
    }
    assert payload["type"] == "sub_agent"
    assert payload["status"] == "completed"


def test_session_send_message_soft_failure() -> None:
    """A malformed message fails SOFT — returns None, never raises."""
    # Bare object args with no usable ``input`` -> soft None.
    args_obj: SubagentSendArgs = {"args": {"purpose": "review"}}
    assert _subagent_message_from_args(args_obj) is None
    # Missing args entirely -> soft None.
    assert _subagent_message_from_args({}) is None
    # String form is extracted unchanged.
    assert _subagent_message_from_args({"args": "do it"}) == "do it"


def test_session_send_model_loud_failure() -> None:
    """A malformed ``model`` fails LOUD — raises ValueError (asymmetry)."""
    bad: SubagentSendArgs = {"args": {"input": "do it", "model": 123}}  # type: ignore[typeddict-item]
    with pytest.raises(ValueError, match="'model' must be a string"):
        _subagent_model_from_args(bad)
    # Absent model is soft (None), proving the loudness is model-malformation
    # specific, not a blanket raise.
    assert _subagent_model_from_args({"args": {"input": "do it"}}) is None
