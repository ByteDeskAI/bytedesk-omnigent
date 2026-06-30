"""Tests for the Automation entity hierarchy (agent-tiering step 1).

Covers the ``infer_category`` truth table, the abstract base / concrete
discriminators, and the structural role Protocols.
"""

from __future__ import annotations

import pytest

from omnigent.entities import (
    SYSTEM_AGENT_NAMES,
    Agent,  # employee concrete lives in entities.agent
    AgentRole,
    Automation,
    SystemAgent,
    Workflow,
    WorkflowRole,
    infer_category,
    is_system,
)


def _mk(cls: type[Automation]) -> Automation:
    return cls(
        id="ag_1",
        created_at=1700000000,
        name="x",
        bundle_location="ag_1/hash",
    )


class TestInferCategory:
    """The single classification function (name + optional params)."""

    def test_allowlist_name_is_system(self) -> None:
        assert infer_category("polly", None) == "system"
        assert infer_category("claude-native-ui", {"workflow": True}) == "system"  # name wins

    def test_workflow_param_is_workflow(self) -> None:
        assert infer_category("some-orchestrator", {"workflow": True}) == "workflow"

    def test_default_is_employee(self) -> None:
        assert infer_category("vivian", {"department": "hr"}) == "employee"
        assert infer_category("vivian", {}) == "employee"

    def test_params_none_never_workflow(self) -> None:
        """Row-only context (no spec) can resolve system/employee but never workflow."""
        assert infer_category("anything", None) == "employee"

    def test_allowlist_membership(self) -> None:
        assert "polly" in SYSTEM_AGENT_NAMES
        assert "debby" in SYSTEM_AGENT_NAMES

    def test_skills_concierge_is_system(self) -> None:
        """The Skill Manager is promoted to a system agent in step 2 (BDP-2577)."""
        assert "skills-concierge" in SYSTEM_AGENT_NAMES
        assert infer_category("skills-concierge", None) == "system"

    def test_goal_commander_is_system(self) -> None:
        """The command-center operator is internal infrastructure, not an employee."""
        assert "goal-commander" in SYSTEM_AGENT_NAMES
        assert infer_category("goal-commander", {"department": "Operations"}) == "system"


class TestIsSystem:
    """``is_system`` reads the role Protocol's category (the seam's first use)."""

    def test_true_for_system_concrete(self) -> None:
        assert is_system(_mk(SystemAgent)) is True

    def test_false_for_employee_and_workflow(self) -> None:
        assert is_system(_mk(Agent)) is False
        assert is_system(_mk(Workflow)) is False


class TestConcretes:
    """Each concrete reports its tier; the base is abstract."""

    def test_categories(self) -> None:
        assert _mk(SystemAgent).category == "system"
        assert _mk(Agent).category == "employee"
        assert _mk(Workflow).category == "workflow"

    def test_all_are_automations(self) -> None:
        for cls in (SystemAgent, Agent, Workflow):
            assert isinstance(_mk(cls), Automation)

    def test_automation_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            Automation(  # type: ignore[abstract]
                id="ag_1", created_at=1, name="x", bundle_location="ag_1/h"
            )


class TestRoleProtocols:
    """The role 'interfaces' are runtime-checkable structural Protocols."""

    def test_agent_satisfies_agent_role(self) -> None:
        assert isinstance(_mk(SystemAgent), AgentRole)
        assert isinstance(_mk(Agent), AgentRole)

    def test_workflow_satisfies_workflow_role(self) -> None:
        assert isinstance(_mk(Workflow), WorkflowRole)
