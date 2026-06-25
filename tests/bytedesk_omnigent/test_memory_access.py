"""Unit tests for the three-tier memory access resolver (BDP-2458).

The resolver is the WHOLE security mechanism: given a tool's address/scope and the
SERVER-VERIFIED caller identity (agent id + department), it returns the
compartment target to use OR an access denial. Owner is always derived from the
verified identity, never the model/body (anti-spoof, ADR-0132/0133/0136).

Rules:
  org:*            -> every agent (read/write)
  dept:<dept>:*    -> only agents whose department == <dept>
  agent:<key>      -> only the calling agent (private; owner = caller id)
"""

from __future__ import annotations

import pytest

from bytedesk_omnigent.memory_access import (
    AccessDenied,
    MemoryTarget,
    resolve_address,
    resolve_prefix,
    resolve_scope_name,
)

VIVIAN = "hr-org-designer"
MAYA = "chief-of-staff"


# ── org tier: everyone ────────────────────────────────────────────────────────


def test_org_address_allows_any_agent() -> None:
    t = resolve_address("org:charter", caller_agent_id=MAYA, caller_department="Operations")
    assert t == MemoryTarget(scope="team", owner="team", name="org-context", key="charter")


def test_org_address_allows_even_with_no_department() -> None:
    t = resolve_address("org:charter", caller_agent_id="x", caller_department=None)
    assert isinstance(t, MemoryTarget) and t.scope == "team"


# ── dept tier: members only ───────────────────────────────────────────────────


def test_dept_address_allows_member() -> None:
    t = resolve_address(
        "dept:engineering:oncall", caller_agent_id="priya", caller_department="Engineering"
    )
    # department is normalized (free-form bundle string -> slug) for matching + name.
    assert t == MemoryTarget(scope="topic", owner="shared", name="dept:engineering", key="oncall")


def test_dept_address_denies_non_member() -> None:
    d = resolve_address(
        "dept:engineering:oncall", caller_agent_id=MAYA, caller_department="Operations"
    )
    assert isinstance(d, AccessDenied)
    assert "engineering" in d.reason.lower()


def test_dept_membership_is_normalized_not_literal() -> None:
    # bundle says "People Operations"; address says "people-operations" -> same dept.
    t = resolve_address(
        "dept:people-operations:goal",
        caller_agent_id=VIVIAN,
        caller_department="People Operations",
    )
    assert isinstance(t, MemoryTarget) and t.name == "dept:people-operations"


def test_dept_denies_when_caller_has_no_department() -> None:
    d = resolve_address("dept:engineering:x", caller_agent_id="y", caller_department=None)
    assert isinstance(d, AccessDenied)


# ── agent tier: private to the caller ─────────────────────────────────────────


def test_agent_address_is_private_to_caller() -> None:
    t = resolve_address("agent:note", caller_agent_id=VIVIAN, caller_department="People Ops")
    # owner is the VERIFIED caller id (never spoofable from the address).
    assert t == MemoryTarget(scope="agent", owner=VIVIAN, name="default", key="note")


def test_agent_address_owner_is_always_the_caller_two_agents_isolated() -> None:
    a = resolve_address("agent:note", caller_agent_id=VIVIAN, caller_department=None)
    b = resolve_address("agent:note", caller_agent_id=MAYA, caller_department=None)
    assert isinstance(a, MemoryTarget) and isinstance(b, MemoryTarget)
    # Same key, DIFFERENT owners -> physically isolated slots (Maya cannot reach Vivian's).
    assert a.owner == VIVIAN and b.owner == MAYA
    assert a.key == b.key == "note"


def test_agent_address_requires_caller_identity() -> None:
    d = resolve_address("agent:note", caller_agent_id=None, caller_department=None)
    assert isinstance(d, AccessDenied)


def test_agent_three_part_address_must_match_caller() -> None:
    # explicit-id form agent:<id>:<key> may only target the caller's own id.
    ok = resolve_address(f"agent:{VIVIAN}:note", caller_agent_id=VIVIAN, caller_department=None)
    assert isinstance(ok, MemoryTarget) and ok.owner == VIVIAN and ok.key == "note"
    bad = resolve_address(f"agent:{VIVIAN}:note", caller_agent_id=MAYA, caller_department=None)
    assert isinstance(bad, AccessDenied)


# ── bad addresses ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    ["", "weird:x", "org:", "dept:eng", "dept::k", "agent:"],
)
def test_malformed_addresses_denied(address: str) -> None:
    d = resolve_address(address, caller_agent_id=VIVIAN, caller_department="People Operations")
    assert isinstance(d, AccessDenied)


# ── prefix (list browse) ──────────────────────────────────────────────────────


def test_prefix_org_allows_any() -> None:
    t = resolve_prefix("org", caller_agent_id=MAYA, caller_department="Operations")
    assert t == MemoryTarget(scope="team", owner="team", name="org-context", key=None)


def test_prefix_dept_member_only() -> None:
    ok = resolve_prefix(
        "dept:engineering", caller_agent_id="priya", caller_department="Engineering"
    )
    assert isinstance(ok, MemoryTarget) and ok.name == "dept:engineering"
    bad = resolve_prefix("dept:engineering", caller_agent_id=MAYA, caller_department="Operations")
    assert isinstance(bad, AccessDenied)


def test_prefix_agent_private() -> None:
    t = resolve_prefix("agent", caller_agent_id=VIVIAN, caller_department=None)
    assert isinstance(t, MemoryTarget) and t.scope == "agent" and t.owner == VIVIAN


# ── scope/name form (search + append, ambient) ────────────────────────────────


def test_scope_name_team_is_org_allow_all() -> None:
    t = resolve_scope_name("team", "org-context", caller_agent_id=MAYA, caller_department="Ops")
    assert t == MemoryTarget(scope="team", owner="team", name="org-context", key=None)


def test_scope_name_topic_dept_enforced() -> None:
    ok = resolve_scope_name(
        "topic", "dept:engineering", caller_agent_id="priya", caller_department="Engineering"
    )
    assert isinstance(ok, MemoryTarget) and ok.owner == "shared"
    bad = resolve_scope_name(
        "topic", "dept:engineering", caller_agent_id=MAYA, caller_department="Operations"
    )
    assert isinstance(bad, AccessDenied)


def test_scope_name_topic_initiative_is_shared_allow_all() -> None:
    # initiative:<id> is an org-wide shared topic, not a department — allowed for all.
    t = resolve_scope_name(
        "topic", "initiative:bdp-2458", caller_agent_id=MAYA, caller_department="Operations"
    )
    assert isinstance(t, MemoryTarget) and t.owner == "shared"


def test_scope_name_agent_is_private() -> None:
    t = resolve_scope_name("agent", "default", caller_agent_id=VIVIAN, caller_department=None)
    assert isinstance(t, MemoryTarget) and t.scope == "agent" and t.owner == VIVIAN
