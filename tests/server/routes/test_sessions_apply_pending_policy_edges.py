"""Edge tests for relay policy ASK write application on approval resolve."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.policies.types import PolicyResult
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import (
    _apply_pending_policy_ask_writes,
    _pending_policy_ask_writes,
    _PendingPolicyAskWrites,
)
from omnigent.spec.types import PolicyAction

_SESSION_ID = "conv_policy_apply"


def _conv() -> Conversation:
    return Conversation(
        id=_SESSION_ID,
        created_at=0,
        updated_at=0,
        root_conversation_id=_SESSION_ID,
        agent_id="ag_test",
    )


@dataclass
class _StubConversationStore:
    conv: Conversation

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self.conv if session_id == self.conv.id else None


@dataclass
class _StubAgentStore:
    def get(self, agent_id: str) -> None:
        raise AssertionError(f"unexpected agent lookup: {agent_id!r}")


@dataclass
class _RecordingPolicyEngine:
    result: PolicyResult
    labels: list[dict[str, str]] = field(default_factory=list)
    state_updates: list[dict[str, object]] = field(default_factory=list)

    def apply_label_writes(self, set_labels: dict[str, str]) -> None:
        self.labels.append(set_labels)

    def apply_state_updates(self, updates: dict[str, object]) -> None:
        self.state_updates.append(updates)


@pytest.fixture(autouse=True)
def _clear_pending_writes() -> None:
    _pending_policy_ask_writes.clear()
    yield
    _pending_policy_ask_writes.clear()


@pytest.mark.asyncio
async def test_apply_pending_policy_ask_writes_noops_without_pending_entry() -> None:
    await _apply_pending_policy_ask_writes(
        session_id=_SESSION_ID,
        conv=_conv(),
        conversation_store=_StubConversationStore(_conv()),  # type: ignore[arg-type]
        agent_store=_StubAgentStore(),  # type: ignore[arg-type]
        data={"elicitation_id": "missing", "action": "accept"},
    )


@pytest.mark.asyncio
async def test_apply_pending_policy_ask_writes_drops_writes_on_decline() -> None:
    eid = "elicit_decline"
    _pending_policy_ask_writes[eid] = _PendingPolicyAskWrites(
        state_updates={"budget": 1},
        set_labels={"risk": "high"},
        from_mcp=False,
    )

    await _apply_pending_policy_ask_writes(
        session_id=_SESSION_ID,
        conv=_conv(),
        conversation_store=_StubConversationStore(_conv()),  # type: ignore[arg-type]
        agent_store=_StubAgentStore(),  # type: ignore[arg-type]
        data={"elicitation_id": eid, "action": "decline"},
    )

    assert eid not in _pending_policy_ask_writes


@pytest.mark.asyncio
async def test_apply_pending_policy_ask_writes_noops_when_spec_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eid = "elicit_no_spec"
    _pending_policy_ask_writes[eid] = _PendingPolicyAskWrites(
        state_updates={"budget": 1},
        set_labels={"risk": "high"},
        from_mcp=False,
    )
    monkeypatch.setattr(sessions_mod, "_load_agent_spec_for_session", lambda *_a, **_k: None)

    await _apply_pending_policy_ask_writes(
        session_id=_SESSION_ID,
        conv=_conv(),
        conversation_store=_StubConversationStore(_conv()),  # type: ignore[arg-type]
        agent_store=_StubAgentStore(),  # type: ignore[arg-type]
        data={"elicitation_id": eid, "action": "accept"},
    )

    assert eid not in _pending_policy_ask_writes


@pytest.mark.asyncio
async def test_apply_pending_policy_ask_writes_applies_labels_and_state_on_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eid = "elicit_apply"
    _pending_policy_ask_writes[eid] = _PendingPolicyAskWrites(
        state_updates={"budget": 42},
        set_labels={"risk": "cleared"},
        from_mcp=False,
    )
    engine = _RecordingPolicyEngine(result=PolicyResult(action=PolicyAction.ALLOW, reason=None))

    monkeypatch.setattr(sessions_mod, "_load_agent_spec_for_session", lambda *_a, **_k: object())
    monkeypatch.setattr(
        sessions_mod,
        "_build_policy_engine_from_spec",
        lambda *_a, **_k: engine,
    )

    await _apply_pending_policy_ask_writes(
        session_id=_SESSION_ID,
        conv=_conv(),
        conversation_store=_StubConversationStore(_conv()),  # type: ignore[arg-type]
        agent_store=_StubAgentStore(),  # type: ignore[arg-type]
        data={"elicitation_id": eid, "action": "accept"},
    )

    assert eid not in _pending_policy_ask_writes
    assert engine.labels == [{"risk": "cleared"}]
    assert engine.state_updates == [{"budget": 42}]
