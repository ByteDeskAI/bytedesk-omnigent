"""Edge-path unit coverage for :mod:`omnigent.runner.cost_judge` helpers."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.llms.types import FunctionCallOutput, MessageOutput, OutputText, Response
from omnigent.runner.cost_judge import (
    LLMJudge,
    _extract_assistant_text,
    _parse_json_object,
    _resolve_judge_model,
    _resolve_workspace_creds,
    build_llm_judge,
)


def test_extract_assistant_text_skips_non_message_output_items() -> None:
    resp = Response(
        output=[
            FunctionCallOutput(call_id="c1", name="grep", arguments="{}"),
            MessageOutput(content=[OutputText(text='{"tier": null}')]),
        ],
        model="test-model",
    )
    assert _extract_assistant_text(resp) == '{"tier": null}'


def test_parse_json_object_rejects_non_object_root() -> None:
    with pytest.raises(ValueError, match="JSON root must be an object"):
        _parse_json_object("[1, 2, 3]")


def test_resolve_judge_model_raises_when_catalog_has_no_models() -> None:
    with pytest.raises(ValueError, match="no models"):
        _resolve_judge_model({"cheap": (), "expensive": ()}, None)


def test_resolve_workspace_creds_delegates_to_databricks_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: sentinel,
    )
    assert _resolve_workspace_creds("prod-profile") is sentinel


@pytest.mark.asyncio
async def test_json_array_verdict_fails_open_after_retry() -> None:
    """A JSON array (non-object root) is treated as malformed and fails open."""

    class _ScriptedClient:
        class _Responses:
            def __init__(self, outer: _ScriptedClient) -> None:
                self._outer = outer

            async def create(self, **kwargs: Any) -> Response:  # type: ignore[explicit-any]
                self._outer.call_count += 1
                return Response(
                    output=[MessageOutput(content=[OutputText(text="[1, 2]")])],
                    model="test-model",
                )

        def __init__(self) -> None:
            self.call_count = 0

        @property
        def responses(self) -> _ScriptedClient._Responses:
            return self._Responses(self)

    client = _ScriptedClient()
    judge: LLMJudge = build_llm_judge(
        tiers={"cheap": ("databricks-claude-haiku-4-5",)},
        executor_config=None,
        connection=None,
        client=client,
    )
    assert await judge.judge(query="hello", turn_anchor="2026-06-10T00:00:00+00:00") is None
    assert client.call_count == 2