"""Integration test: memory tools execute SERVER-SIDE in the tools/call handler.

Drives the real :func:`_handle_mcp_tools_call` (the MCP proxy choke point) with a
fake memory store, proving the BDP-2458 wiring:

* ``memory__*`` returns a result with ``runner_router=None`` — it NEVER dispatches
  to the runner (the whole point: identity can't ride the shared stdio front).
* the owner is stamped from ``conv.agent_id`` + the agent bundle's department, so
  org resolves cross-agent while agent scope stays private — using two real
  conversations bound to two different agents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import _handle_mcp_tools_call
from omnigent.spec.types import PolicyAction


@dataclass
class _StubSpec:
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class _StubConversationStore:
    convs: dict[str, Conversation]

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self.convs.get(session_id)


@dataclass
class _AllowEngine:
    async def evaluate(self, ctx: EvaluationContext) -> PolicyResult:
        return PolicyResult(action=PolicyAction.ALLOW)

    def apply_label_writes(self, set_labels: dict[str, str]) -> None:
        return None


class _FakeStore:
    def __init__(self) -> None:
        self.slots: dict[tuple, dict] = {}
        self._seq = 0

    def write(self, *, scope, owner, name, content, weight=1.0, key=None, **kw):
        self._seq += 1
        mid = f"mem_{self._seq}"
        if key is not None:
            self.slots[(scope, owner, name, key)] = {
                "memory_id": mid, "content": content, "weight": weight,
                "created_at": 0, "confidence": kw.get("confidence"),
                "source_conversation_id": kw.get("source_conversation_id"),
            }
        return mid

    def archive_keyed(self, *, scope, owner, name, key) -> int:
        return 1 if self.slots.pop((scope, owner, name, key), None) is not None else 0

    def get_keyed(self, *, scope, owner, name, key):
        row = self.slots.get((scope, owner, name, key))
        return dict(row) if row is not None else None

    def list_keyed(self, *, scope, owner, name):
        return [
            {"key": k, "content": v["content"], "weight": v["weight"]}
            for (s, o, n, k), v in self.slots.items() if (s, o, n) == (scope, owner, name)
        ]

    def recall(self, *, scope, owner, name, query, k=10, kind="all"):
        return []

    def note_recalled(self, hits) -> None:
        return None

    @property
    def store(self):
        return self


def _conv(session_id: str, agent_id: str) -> Conversation:
    return Conversation(
        id=session_id, created_at=0, updated_at=0,
        root_conversation_id=session_id, agent_id=agent_id,
    )


def _ok_text(response: Any) -> dict:
    """Extract the tool's JSON result from an MCP ok response."""
    payload = json.loads(bytes(response.body))
    assert "result" in payload, payload
    return json.loads(payload["result"]["content"][0]["text"])


@pytest.fixture()
def wired(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: store)
    monkeypatch.setattr("omnigent.runtime.get_memory_store", lambda: store)
    monkeypatch.setattr(
        sessions_mod, "_build_policy_engine_from_spec",
        lambda spec, session_id, conversation_store: _AllowEngine(),
    )
    return store


async def _call(convs, session_id, name, arguments):
    return await _handle_mcp_tools_call(
        rpc_id=1,
        session_id=session_id,
        params={"name": name, "arguments": arguments},
        conversation_store=_StubConversationStore(convs),  # type: ignore[arg-type]
        agent_store=object(),  # type: ignore[arg-type]
        runner_router=None,  # ← proves memory never dispatches to the runner
    )


@pytest.mark.asyncio
async def test_org_memory_resolves_cross_agent_without_runner(monkeypatch, wired) -> None:
    # Two agents in different departments.
    vivian = _conv("conv_v", "hr-org-designer")
    maya = _conv("conv_m", "chief-of-staff")
    convs = {"conv_v": vivian, "conv_m": maya}

    def spec_for(conv, agent_store):
        dept = {"hr-org-designer": "People Operations", "chief-of-staff": "Operations"}
        return _StubSpec(params={"department": dept[conv.agent_id]})

    monkeypatch.setattr(sessions_mod, "_load_agent_spec_for_session", spec_for)

    put = _ok_text(await _call(convs, "conv_v", "memory__put",
                               {"address": "org:charter", "content": "ship weekly"}))
    assert "memory_id" in put

    got = _ok_text(await _call(convs, "conv_m", "memory__get", {"address": "org:charter"}))
    assert got["found"] is True and got["content"] == "ship weekly"


@pytest.mark.asyncio
async def test_agent_scope_is_private_across_agents_via_handler(monkeypatch, wired) -> None:
    vivian = _conv("conv_v", "hr-org-designer")
    maya = _conv("conv_m", "chief-of-staff")
    convs = {"conv_v": vivian, "conv_m": maya}
    monkeypatch.setattr(
        sessions_mod, "_load_agent_spec_for_session",
        lambda conv, agent_store: _StubSpec(params={}),
    )

    put = _ok_text(await _call(convs, "conv_v", "memory__put",
                               {"address": "agent:note", "content": "vivian secret"}))
    assert "memory_id" in put
    mine = _ok_text(await _call(convs, "conv_v", "memory__get", {"address": "agent:note"}))
    assert mine["found"] is True
    # Maya addresses the same 'agent:note' but reaches her OWN (empty) compartment.
    theirs = _ok_text(await _call(convs, "conv_m", "memory__get", {"address": "agent:note"}))
    assert theirs["found"] is False


@pytest.mark.asyncio
async def test_dept_membership_enforced_via_handler(monkeypatch, wired) -> None:
    priya = _conv("conv_p", "backend-development-lead")
    maya = _conv("conv_m", "chief-of-staff")
    convs = {"conv_p": priya, "conv_m": maya}

    def spec_for(conv, agent_store):
        dept = {"backend-development-lead": "Engineering", "chief-of-staff": "Operations"}
        return _StubSpec(params={"department": dept[conv.agent_id]})

    monkeypatch.setattr(sessions_mod, "_load_agent_spec_for_session", spec_for)

    _ok_text(await _call(convs, "conv_p", "memory__put",
                         {"address": "dept:engineering:oncall", "content": "Priya"}))
    # Maya (Operations) is denied.
    denied = _ok_text(await _call(convs, "conv_m", "memory__get",
                                  {"address": "dept:engineering:oncall"}))
    assert "error" in denied and "engineering" in denied["error"].lower()
