"""Contracts for the internal agent communication package."""

from __future__ import annotations

from omnigent.communications import (
    ChatActor,
    ChatActorKind,
    ChatCommandResult,
    ChildSessionUpdated,
    DelegateToAgentCommand,
    DelegationResult,
    DispatchDisposition,
    InputAccepted,
    PostSessionEventCommand,
    SessionStatus,
    SessionStatusChanged,
    StartSessionCommand,
)


def test_chat_command_contracts_capture_start_post_and_delegation_intent() -> None:
    actor = ChatActor(kind=ChatActorKind.USER, user_id="user_1", tenant_id="tenant_1")

    start = StartSessionCommand(
        agent_id="ag_1",
        actor=actor,
        title="Design site",
        initial_message="Create a visual direction",
        labels={"client": "acme"},
        idempotency_key="idem_1",
    )
    post = PostSessionEventCommand(
        session_id="conv_1",
        actor=actor,
        event_type="message",
        payload={"content": "Implement it"},
        request_id="req_1",
    )
    delegate = DelegateToAgentCommand(
        parent_session_id="conv_1",
        actor=actor,
        agent_name="designer",
        title="Visual design",
        prompt="Generate hero options",
        metadata={"step": "design"},
    )

    assert start.agent_id == "ag_1"
    assert start.labels["client"] == "acme"
    assert post.payload["content"] == "Implement it"
    assert delegate.reuse_existing is True
    assert delegate.metadata["step"] == "design"


def test_chat_result_and_domain_event_contracts_share_status_vocabulary() -> None:
    result = ChatCommandResult(
        session_id="conv_1",
        disposition=DispatchDisposition.FORWARDED,
        item_ids=("item_1",),
        response_id="resp_1",
    )
    delegation = DelegationResult(
        parent_session_id="conv_1",
        child_session_id="conv_2",
        disposition=DispatchDisposition.ACCEPTED,
        handle_id="handle_1",
    )
    status = SessionStatusChanged(
        session_id="conv_1",
        previous_status=SessionStatus.RUNNING,
        status=SessionStatus.WAITING,
    )
    accepted = InputAccepted(session_id="conv_1", item_id="item_1", pending_id="pending_1")
    child = ChildSessionUpdated(
        parent_session_id="conv_1",
        child_session_id="conv_2",
        status=SessionStatus.LAUNCHING,
        title="Visual design",
    )

    assert result.disposition == DispatchDisposition.FORWARDED
    assert result.item_ids == ("item_1",)
    assert delegation.child_session_id == "conv_2"
    assert status.status == "waiting"
    assert accepted.pending_id == "pending_1"
    assert child.status == SessionStatus.LAUNCHING
