"""Unit tests for server-side memory-tool execution (BDP-2458).

Drives ``execute_memory_tool`` against an in-memory fake store/provider so the
identity-stamped owner + the three-tier access rules are provable offline. The
fake keys keyed slots by ``(scope, owner, name, key)`` exactly like the real
store, so cross-agent isolation (agent scope) and shared visibility (org/dept)
are real, not asserted.
"""

from __future__ import annotations

import json

import pytest

from bytedesk_omnigent import memory_tool_intercept as mti

VIVIAN = "hr-org-designer"
MAYA = "chief-of-staff"
PRIYA = "backend-development-lead"
ELIAS = "platform-architect"


class _FakeStore:
    """Keyed-slot store: dict[(scope, owner, name, key)] -> content."""

    def __init__(self) -> None:
        self.slots: dict[tuple, dict] = {}
        self._seq = 0

    def _mid(self) -> str:
        self._seq += 1
        return f"mem_{self._seq}"

    # provider.write(key=...) routes here in the fake (we collapse provider+store).
    def write(self, *, scope, owner, name, content, weight=1.0, key=None, **kw):
        mid = self._mid()
        if key is not None:
            self.slots[(scope, owner, name, key)] = {
                "memory_id": mid,
                "content": content,
                "weight": weight,
                "created_at": 0,
                "confidence": kw.get("confidence"),
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
            for (s, o, n, k), v in self.slots.items()
            if (s, o, n) == (scope, owner, name)
        ]

    # provider.recall / note_recalled (ambient search — not exercised heavily here).
    def recall(self, *, scope, owner, name, query, k=10, kind="all"):
        return []

    def note_recalled(self, hits) -> None:
        return None

    # get_memory_store() returns this same object in the fake.
    @property
    def store(self):
        return self


@pytest.fixture()
def fake(monkeypatch):
    s = _FakeStore()
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: s)
    monkeypatch.setattr("omnigent.runtime.get_memory_store", lambda: s)
    return s


def _call(tool, args, *, agent, dept):
    return json.loads(
        mti.execute_memory_tool(tool, args, caller_agent_id=agent, caller_department=dept)
    )


# ── is_memory_tool ────────────────────────────────────────────────────────────


def test_is_memory_tool_recognizes_and_rejects() -> None:
    assert mti.is_memory_tool("memory__get")
    assert mti.is_memory_tool("memory__put")
    assert not mti.is_memory_tool("github__search")
    assert not mti.is_memory_tool("memory__wat")
    assert not mti.is_memory_tool("sys_os_read")


# ── ORG: Vivian writes, Maya resolves (cross-agent) ───────────────────────────


def test_org_put_then_cross_agent_get(fake) -> None:
    put = _call("memory__put", {"address": "org:charter", "content": "ship weekly"},
                agent=VIVIAN, dept="People Operations")
    assert "memory_id" in put
    got = _call("memory__get", {"address": "org:charter"}, agent=MAYA, dept="Operations")
    assert got["found"] is True and got["content"] == "ship weekly"


# ── DEPT: same-department resolve; cross-department denied ─────────────────────


def test_dept_put_then_same_dept_get_and_cross_dept_denied(fake) -> None:
    put = _call("memory__put", {"address": "dept:engineering:oncall", "content": "Priya primary"},
                agent=PRIYA, dept="Engineering")
    assert "memory_id" in put
    # Elias (also Engineering) resolves it.
    ok = _call(
        "memory__get", {"address": "dept:engineering:oncall"}, agent=ELIAS, dept="Engineering"
    )
    assert ok["found"] is True and ok["content"] == "Priya primary"
    # Maya (Operations) is denied — not a member.
    denied = _call(
        "memory__get", {"address": "dept:engineering:oncall"}, agent=MAYA, dept="Operations"
    )
    assert "error" in denied and "engineering" in denied["error"].lower()


def test_dept_put_denied_for_non_member(fake) -> None:
    denied = _call("memory__put", {"address": "dept:engineering:x", "content": "nope"},
                   agent=MAYA, dept="Operations")
    assert "error" in denied
    # nothing was written
    check = _call(
        "memory__get", {"address": "dept:engineering:x"}, agent=PRIYA, dept="Engineering"
    )
    assert check["found"] is False


# ── AGENT: private; another agent cannot resolve ──────────────────────────────


def test_agent_scope_is_private_across_agents(fake) -> None:
    put = _call("memory__put", {"address": "agent:note", "content": "vivian secret"},
                agent=VIVIAN, dept="People Operations")
    assert "memory_id" in put
    # Vivian reads her own.
    mine = _call("memory__get", {"address": "agent:note"}, agent=VIVIAN, dept="People Operations")
    assert mine["found"] is True and mine["content"] == "vivian secret"
    # Maya addressing 'agent:note' reads HER OWN (empty), never Vivian's.
    others = _call("memory__get", {"address": "agent:note"}, agent=MAYA, dept="Operations")
    assert others["found"] is False


def test_agent_cannot_address_another_agents_id(fake) -> None:
    _call("memory__put", {"address": "agent:note", "content": "v"}, agent=VIVIAN, dept=None)
    denied = _call("memory__get", {"address": f"agent:{VIVIAN}:note"}, agent=MAYA, dept=None)
    assert "error" in denied


# ── list + unset round trips ──────────────────────────────────────────────────


def test_list_and_unset_org(fake) -> None:
    _call("memory__put", {"address": "org:charter", "content": "c"}, agent=VIVIAN, dept=None)
    listed = _call("memory__list", {"prefix": "org"}, agent=MAYA, dept="Operations")
    assert any(s["key"] == "charter" for s in listed["slots"])
    cleared = _call("memory__unset", {"address": "org:charter"}, agent=MAYA, dept="Operations")
    assert cleared.get("cleared") == 1
    gone = _call("memory__get", {"address": "org:charter"}, agent=VIVIAN, dept=None)
    assert gone["found"] is False
