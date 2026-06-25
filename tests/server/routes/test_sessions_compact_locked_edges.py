"""Edge tests for explicit session compaction under _run_compact_locked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omnigent.entities import Agent, Conversation, LoadedAgent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import sessions as sessions_mod
from omnigent.spec.types import AgentSpec, ExecutorSpec, LLMConfig


def _conv(*, agent_id: str | None = "ag_compact") -> Conversation:
    return Conversation(
        id="conv_compact",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_compact",
        agent_id=agent_id,
    )


@pytest.fixture(autouse=True)
def _clear_status_cache() -> None:
    sessions_mod._session_status_cache.clear()
    yield
    sessions_mod._session_status_cache.clear()


@pytest.mark.asyncio
async def test_run_compact_locked_errors_without_agent_binding() -> None:
    with pytest.raises(OmnigentError) as exc_info:
        await sessions_mod._run_compact_locked(
            "conv_compact",
            _conv(agent_id=None),
            MagicMock(),
            MagicMock(),
        )

    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert "no agent binding" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_compact_locked_errors_without_agent_cache() -> None:
    with pytest.raises(OmnigentError) as exc_info:
        await sessions_mod._run_compact_locked(
            "conv_compact",
            _conv(),
            MagicMock(),
            None,
        )

    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert "agent cache is not configured" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_compact_locked_errors_while_turn_running() -> None:
    sessions_mod._session_status_cache["conv_compact"] = "running"

    with pytest.raises(OmnigentError) as exc_info:
        await sessions_mod._run_compact_locked(
            "conv_compact",
            _conv(),
            MagicMock(),
            MagicMock(),
        )

    assert exc_info.value.code == ErrorCode.CONFLICT
    assert "Cannot compact while a turn is running" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_compact_locked_errors_when_agent_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_store = MagicMock()
    agent_store.get.return_value = None

    with pytest.raises(OmnigentError) as exc_info:
        await sessions_mod._run_compact_locked(
            "conv_compact",
            _conv(),
            agent_store,
            MagicMock(),
        )

    assert exc_info.value.code == ErrorCode.NOT_FOUND
    assert "ag_compact" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_compact_locked_errors_without_llm_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(
        id="ag_compact",
        created_at=0,
        name="worker",
        bundle_location="bundle/key",
    )
    agent_store = MagicMock()
    agent_store.get.return_value = agent

    spec = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    agent_cache = MagicMock()
    agent_cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/worker"))

    with pytest.raises(OmnigentError) as exc_info:
        await sessions_mod._run_compact_locked(
            "conv_compact",
            _conv(),
            agent_store,
            agent_cache,
        )

    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    assert "requires a configured LLM model" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_compact_locked_uses_executor_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(
        id="ag_compact",
        created_at=0,
        name="worker",
        bundle_location="bundle/key",
    )
    agent_store = MagicMock()
    agent_store.get.return_value = agent

    spec = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(
            type="omnigent",
            model="openai/gpt-4.1",
            connection={"api_key": "sk-test"},
            config={"harness": "openai-agents"},
        ),
    )
    agent_cache = MagicMock()
    agent_cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/worker"))

    compact_calls: list[dict[str, object]] = []
    status_calls: list[str] = []

    async def _compact(**kwargs: object) -> None:
        compact_calls.append(kwargs)

    monkeypatch.setattr(
        "omnigent.runtime.workflow.compact_conversation_now",
        _compact,
    )
    monkeypatch.setattr(
        sessions_mod,
        "_publish_status",
        lambda session_id, status: status_calls.append(status),
    )

    await sessions_mod._run_compact_locked(
        "conv_compact",
        _conv(),
        agent_store,
        agent_cache,
    )

    assert len(compact_calls) == 1
    llm_config = compact_calls[0]["llm_config"]
    assert isinstance(llm_config, LLMConfig)
    assert llm_config.model == "openai/gpt-4.1"
    assert status_calls == ["running", "idle"]


@pytest.mark.asyncio
async def test_run_compact_locked_publishes_failure_on_compact_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(
        id="ag_compact",
        created_at=0,
        name="worker",
        bundle_location="bundle/key",
    )
    agent_store = MagicMock()
    agent_store.get.return_value = agent

    spec = AgentSpec(
        spec_version=1,
        name="worker",
        llm=LLMConfig(model="openai/gpt-4.1"),
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    agent_cache = MagicMock()
    agent_cache.load.return_value = LoadedAgent(spec=spec, workdir=Path("/tmp/worker"))

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("summary failed")

    published_failed: list[str] = []
    status_calls: list[str] = []

    monkeypatch.setattr(
        "omnigent.runtime.workflow.compact_conversation_now",
        _boom,
    )
    monkeypatch.setattr(
        sessions_mod,
        "_publish_compaction_failed",
        lambda session_id: published_failed.append(session_id),
    )
    monkeypatch.setattr(
        sessions_mod,
        "_publish_status",
        lambda _session_id, status: status_calls.append(status),
    )

    with pytest.raises(OmnigentError) as exc_info:
        await sessions_mod._run_compact_locked(
            "conv_compact",
            _conv(),
            agent_store,
            agent_cache,
        )

    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert "summary failed" in str(exc_info.value)
    assert published_failed == ["conv_compact"]
    assert status_calls == ["running", "idle"]
