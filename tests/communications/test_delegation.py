"""Tests for class-driven child-session delegation services."""

from __future__ import annotations

import pytest

from omnigent.communications import (
    ChatActor,
    ChatActorKind,
    ChildSessionDelegationService,
    DelegateToAgentCommand,
)


def command() -> DelegateToAgentCommand:
    """Build a minimal child delegation command."""
    return DelegateToAgentCommand(
        parent_session_id="conv_parent",
        actor=ChatActor(kind=ChatActorKind.USER, user_id="alice@example.com"),
        agent_id="ag_child",
        agent_name="designer",
        title="blueprint:design",
        prompt="Draft the landing page",
        labels={"omnigent.blueprint.node_id": "design"},
    )


@pytest.mark.asyncio
async def test_child_session_delegation_service_runs_create_send_read_record_flow() -> None:
    created: list[DelegateToAgentCommand] = []
    posted: list[tuple[str, str, ChatActor]] = []
    recorded: list[tuple[DelegateToAgentCommand, str, str]] = []

    async def create_child_session(cmd: DelegateToAgentCommand) -> str:
        created.append(cmd)
        return "conv_child"

    async def post_child_prompt(child_session_id: str, prompt: str, actor: ChatActor) -> None:
        posted.append((child_session_id, prompt, actor))

    async def read_child_output(child_session_id: str) -> str:
        assert child_session_id == "conv_child"
        return '{"approved": true}'

    async def record_child_return(
        cmd: DelegateToAgentCommand,
        child_session_id: str,
        raw_output: str,
    ) -> None:
        recorded.append((cmd, child_session_id, raw_output))

    service = ChildSessionDelegationService(
        create_child_session=create_child_session,
        post_child_prompt=post_child_prompt,
        read_child_output=read_child_output,
        record_child_return=record_child_return,
        parse_child_output=lambda _cmd, raw: ("completed", {"raw": raw}, None),
    )

    cmd = command()
    outcome = await service.delegate(cmd)

    assert created == [cmd]
    assert posted == [("conv_child", "Draft the landing page", cmd.actor)]
    assert recorded == [(cmd, "conv_child", '{"approved": true}')]
    assert outcome.status == "completed"
    assert outcome.child_session_id == "conv_child"
    assert outcome.output == {"raw": '{"approved": true}'}
    assert outcome.raw_output == '{"approved": true}'


@pytest.mark.asyncio
async def test_child_session_delegation_service_returns_waiting_for_runner_unavailable() -> None:
    class RunnerUnavailable(Exception):
        """Test-only runner unavailable error."""

    read_calls: list[str] = []

    async def create_child_session(_cmd: DelegateToAgentCommand) -> str:
        return "conv_child"

    async def post_child_prompt(_child_session_id: str, _prompt: str, _actor: ChatActor) -> None:
        raise RunnerUnavailable("runner down")

    async def read_child_output(child_session_id: str) -> str:
        read_calls.append(child_session_id)
        return "should not be read"

    service = ChildSessionDelegationService(
        create_child_session=create_child_session,
        post_child_prompt=post_child_prompt,
        read_child_output=read_child_output,
        is_runner_unavailable=lambda exc: isinstance(exc, RunnerUnavailable),
    )

    outcome = await service.delegate(command())

    assert outcome.status == "waiting"
    assert outcome.child_session_id == "conv_child"
    assert outcome.output == {"prompt": "Draft the landing page"}
    assert read_calls == []


@pytest.mark.asyncio
async def test_child_session_delegation_service_reraises_non_runner_errors() -> None:
    class BadPrompt(Exception):
        """Test-only prompt failure."""

    async def create_child_session(_cmd: DelegateToAgentCommand) -> str:
        return "conv_child"

    async def post_child_prompt(_child_session_id: str, _prompt: str, _actor: ChatActor) -> None:
        raise BadPrompt("bad prompt")

    async def read_child_output(_child_session_id: str) -> str:
        return "should not be read"

    service = ChildSessionDelegationService(
        create_child_session=create_child_session,
        post_child_prompt=post_child_prompt,
        read_child_output=read_child_output,
        is_runner_unavailable=lambda _exc: False,
    )

    with pytest.raises(BadPrompt, match="bad prompt"):
        await service.delegate(command())
