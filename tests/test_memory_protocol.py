"""Tests for the org-memory protocol conventions (BDP-2276 D6/E1, ADR-0142)."""

from __future__ import annotations

import pytest

from omnigent.memory_protocol import (
    ORG_CONTEXT_COMPARTMENT,
    ORG_CONTEXT_SCOPE,
    ensure_org_compartments,
    initiative_compartment,
)


def test_initiative_compartment_slugifies() -> None:
    assert initiative_compartment("BDP-2276") == "initiative:bdp-2276"
    assert initiative_compartment("  Q3 Launch!! ") == "initiative:q3-launch"


def test_initiative_compartment_is_stable_for_equivalent_ids() -> None:
    assert initiative_compartment("BDP-2276") == initiative_compartment("bdp_2276")


def test_initiative_compartment_rejects_empty() -> None:
    with pytest.raises(ValueError):
        initiative_compartment("!!!")


def test_ensure_org_compartments_adds_when_absent() -> None:
    out = ensure_org_compartments([{"scope": "agent", "name": "notes"}])
    assert out[0] == {"scope": ORG_CONTEXT_SCOPE, "name": ORG_CONTEXT_COMPARTMENT}
    assert {"scope": "agent", "name": "notes"} in out


def test_ensure_org_compartments_idempotent_when_present() -> None:
    existing = [
        {"scope": ORG_CONTEXT_SCOPE, "name": ORG_CONTEXT_COMPARTMENT},
        {"scope": "agent", "name": "notes"},
    ]
    out = ensure_org_compartments(existing)
    names = [(c["scope"], c["name"]) for c in out]
    assert names.count((ORG_CONTEXT_SCOPE, ORG_CONTEXT_COMPARTMENT)) == 1
    assert len(out) == 2


def test_ensure_org_compartments_does_not_mutate_input() -> None:
    src = [{"scope": "agent", "name": "notes"}]
    ensure_org_compartments(src)
    assert src == [{"scope": "agent", "name": "notes"}]
