"""Typing-contract test for EvaluationContext.content phase shapes (BDP-2366).

The ``content`` field stays annotated ``Any`` (the many per-phase call sites
build different shapes), but the documented per-phase union is named as
:data:`~omnigent.policies.types.PhaseContent` with TypedDicts for the two
structured phases. This pins the representative shapes so the documented
contract cannot silently drift; the union is also enforced statically by mypy
wherever ``PhaseContent`` is used.
"""

from __future__ import annotations

from typing import Any

from omnigent.policies.types import (
    PhaseContent,
    ToolCallContent,
    ToolResultContent,
)


def _accepts_phase_content(content: PhaseContent) -> PhaseContent:
    """Typed sink: only a structural ``PhaseContent`` member is assignable."""
    return content


def test_request_phase_content_is_str() -> None:
    """REQUEST / RESPONSE phases carry raw text (the ``str`` member)."""
    assert _accepts_phase_content("hello") == "hello"


def test_tool_call_content_conforms() -> None:
    """TOOL_CALL content conforms to ToolCallContent."""
    content: ToolCallContent = {"name": "sys_session_send", "arguments": {"agent": "x"}}
    assert _accepts_phase_content(content)["name"] == "sys_session_send"


def test_tool_result_content_conforms() -> None:
    """TOOL_RESULT content conforms to ToolResultContent."""
    content: ToolResultContent = {"result": {"ok": True}}
    assert _accepts_phase_content(content)["result"] == {"ok": True}


def test_llm_phase_content_is_open_dict() -> None:
    """LLM_REQUEST / LLM_RESPONSE phases carry an opaque dict body."""
    body: dict[str, Any] = {"messages": [], "tools": []}
    assert _accepts_phase_content(body) == body
