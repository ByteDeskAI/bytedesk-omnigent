"""Unit tests for :mod:`omnigent.runtime.prompt`."""

from __future__ import annotations

from omnigent.entities import (
    CompactionData,
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NativeToolData,
    ReasoningData,
)
from omnigent.runtime.prompt import build_instructions, history_to_input_items
from omnigent.spec import AgentSpec
from omnigent.spec.types import ExecutorSpec, SkillSpec


def _minimal_spec(**overrides: object) -> AgentSpec:
    """Build a minimal valid AgentSpec with optional overrides."""
    defaults: dict[str, object] = {
        "spec_version": 1,
        "executor": ExecutorSpec(config={"harness": "openai-agents-sdk"}),
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)  # type: ignore[arg-type]


def test_build_instructions_uses_spec_and_per_request_parts() -> None:
    """Base instructions and per-request text are joined with blank lines."""
    spec = _minimal_spec(instructions="You are a coder.")
    result = build_instructions(spec, "Focus on tests.", tool_schemas=[])
    assert result == "You are a coder.\n\nFocus on tests."


def test_build_instructions_default_when_empty() -> None:
    """An empty spec yields the default helpful-assistant prompt."""
    spec = _minimal_spec()
    assert build_instructions(spec, None, tool_schemas=[]) == "You are a helpful assistant."


def test_build_instructions_lists_skills_when_load_skill_available() -> None:
    """Skills are mentioned only when ``load_skill`` is in the tool schemas."""
    spec = _minimal_spec(
        skills=[
            SkillSpec(name="code-review", description="Review code", content="Review steps."),
        ]
    )
    schemas = [{"type": "function", "function": {"name": "load_skill", "parameters": {}}}]
    result = build_instructions(spec, None, tool_schemas=schemas)
    assert "Available skills (use the load_skill tool to load one):" in result
    assert "- code-review: Review code" in result


def test_build_instructions_omits_skills_without_load_skill_tool() -> None:
    """Skills are not listed when the executor handles them natively."""
    spec = _minimal_spec(
        skills=[SkillSpec(name="lint", description="Lint code", content="Lint steps.")],
    )
    result = build_instructions(spec, None, tool_schemas=[])
    assert "Available skills" not in result
    assert "lint" not in result


def test_history_to_input_items_maps_message_and_strips_annotations() -> None:
    """Message items pass through with output_text annotations removed."""
    items = [
        ConversationItem(
            id="msg_1",
            type="message",
            status="completed",
            response_id="resp_1",
            created_at=1,
            data=MessageData(
                role="assistant",
                agent="agent-a",
                content=[
                    {
                        "type": "output_text",
                        "text": "See file",
                        "annotations": [{"type": "file_citation", "file_id": "f1"}],
                    }
                ],
            ),
        )
    ]
    result = history_to_input_items(items)
    assert result == [
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "See file"}],
        }
    ]


def test_history_to_input_items_maps_function_call_and_output() -> None:
    """Function call items map to Responses API function_call shapes."""
    items = [
        ConversationItem(
            id="fc_1",
            type="function_call",
            status="completed",
            response_id="resp_1",
            created_at=1,
            data=FunctionCallData(
                agent="agent",
                name="search",
                arguments='{"q": "x"}',
                call_id="call_1",
            ),
        ),
        ConversationItem(
            id="fco_1",
            type="function_call_output",
            status="completed",
            response_id="resp_1",
            created_at=2,
            data=FunctionCallOutputData(call_id="call_1", output='{"ok": true}'),
        ),
    ]
    result = history_to_input_items(items)
    assert result == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q": "x"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok": true}',
        },
    ]


def test_history_to_input_items_preserves_non_annotation_blocks() -> None:
    """Content blocks without output annotations pass through unchanged."""
    items = [
        ConversationItem(
            id="msg_2",
            type="message",
            status="completed",
            response_id="resp_1",
            created_at=1,
            data=MessageData(
                role="user",
                content=[{"type": "input_text", "text": "hello"}],
            ),
        )
    ]
    result = history_to_input_items(items)
    assert result == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]


def test_history_to_input_items_passes_native_tool_and_skips_metadata_types() -> None:
    """Native tool items pass through; reasoning and compaction are omitted."""
    native_item = {"type": "web_search_call", "id": "ws_1"}
    items = [
        ConversationItem(
            id="nt_1",
            type="native_tool",
            status="completed",
            response_id="resp_1",
            created_at=1,
            data=NativeToolData(item=native_item),
        ),
        ConversationItem(
            id="r_1",
            type="reasoning",
            status="completed",
            response_id="resp_1",
            created_at=2,
            data=ReasoningData(
                agent="agent-a",
                summary=[{"type": "summary_text", "text": "thinking"}],
            ),
        ),
        ConversationItem(
            id="c_1",
            type="compaction",
            status="completed",
            response_id="resp_1",
            created_at=3,
            data=CompactionData(
                summary="old context",
                last_item_id="msg_0",
                model="gpt-test",
                token_count=10,
            ),
        ),
    ]
    result = history_to_input_items(items)
    assert result == [native_item]