"""Automation entity hierarchy — agent tiering, step 1.

Every persisted agent row becomes one of three concrete entities so the three
tiers are a real type hierarchy, not a loose string:

- :class:`SystemAgent` — platform-shipped, tightly-controlled (e.g. the Skill
  Manager). Classified by the :data:`SYSTEM_AGENT_NAMES` allowlist.
- :class:`Agent` — a regular employee agent (the default). Lives in
  ``omnigent/entities/agent.py`` to keep the widely-imported name where callers
  expect it.
- :class:`Workflow` — an orchestrator *definition* (``params.workflow: true``).
  Its ``task_wf_*`` row and live spawn-tree are runtime projections, not the
  entity (decision: Workflow stays in the agent store).

:class:`Automation` is the shared abstract base (house idiom: ``abc.ABC`` +
``@abstractmethod``, like :class:`omnigent.tools.base.Tool`). :class:`AgentRole`
and :class:`WorkflowRole` are the role "interfaces" — structural
``@runtime_checkable`` Protocols (house idiom: ``omnigent/identity/ports.py``;
no ``I`` prefix). They are the seam step 2 (Skill Manager, authz gate, UI
grouping) builds on; nothing consumes them yet.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

AgentCategory = Literal["system", "employee", "workflow"]

# Bootstrap "system" allowlist — privilege classification, NOT SoT ownership
# (do not infer from ``sot_tier``: that means omnigent-is-source-of-truth, a
# different axis). Names mirror the seeded built-ins in omnigent/server/app.py
# (the four NativeCodingAgent.agent_name values + debby + polly) plus the Skill
# Manager (skills-concierge), promoted to a system agent in step 2 (BDP-2577) so
# its ``system.skills.manage`` privilege gates cross-agent skill installs.
SYSTEM_AGENT_NAMES: frozenset[str] = frozenset(
    {
        "claude-native-ui",
        "codex-native-ui",
        "pi-native-ui",
        "grok-native-ui",
        "debby",
        "polly",
        "skills-concierge",
    }
)


def infer_category(name: str, params: dict | None) -> AgentCategory:
    """Classify an agent from its name + (optional) bundle params.

    ``system`` if the name is on the allowlist; ``workflow`` if
    ``params.workflow`` is truthy; ``employee`` otherwise. ``params=None`` is the
    row-only context (the converter has no spec): it can resolve system/employee
    but never ``workflow`` (that needs the spec), which is why the post-seed
    backfill persists the column for workflow rows.

    :param name: The agent's unique name, e.g. ``"polly"``.
    :param params: The bundle's ``params`` dict, or ``None`` when unavailable.
    :returns: The inferred :data:`AgentCategory`.
    """
    if name in SYSTEM_AGENT_NAMES:
        return "system"
    if params and params.get("workflow"):
        return "workflow"
    return "employee"


@runtime_checkable
class AgentRole(Protocol):
    """Role interface for person-like automations (system or employee).

    Structural: any object exposing these attributes satisfies it without
    subclassing. Seam for step 2 — not yet consumed.
    """

    id: str
    name: str

    @property
    def category(self) -> AgentCategory: ...


@runtime_checkable
class WorkflowRole(Protocol):
    """Role interface for orchestrator-definition automations (workflows)."""

    id: str
    name: str

    @property
    def category(self) -> AgentCategory: ...


def is_system(agent: AgentRole) -> bool:
    """Return whether *agent* is a system-tier automation (step 2, BDP-2577).

    The first real consumer of the role Protocols: reads the structural
    :attr:`AgentRole.category` rather than re-checking the name allowlist, so
    callers classify via the entity seam. Used by the startup classification
    backfill to keep system classification independent of a (possibly
    failed-to-load) bundle spec — the converter already derives ``"system"``
    from the allowlisted name.

    :param agent: Any object satisfying :class:`AgentRole`.
    :returns: ``True`` if its category is ``"system"``.
    """
    return agent.category == "system"


@dataclass
class Automation(abc.ABC):
    """Shared abstract base for every registered automation.

    Holds the persisted row fields (formerly on :class:`Agent`). Concrete
    subclasses supply :attr:`category`; instantiating ``Automation`` directly
    raises (it's abstract).

    :param id: Unique agent identifier, e.g. ``"ag_abc123"``.
    :param created_at: Unix epoch seconds of creation.
    :param name: Human-readable name. Template agents have unique names;
        session-scoped copies may reuse names across sessions.
    :param bundle_location: Content-addressed artifact store key.
    :param version: Monotonic version counter (starts at 1).
    :param description: Optional free-text description.
    :param updated_at: Unix epoch seconds of last update, or ``None``.
    :param session_id: Owning conversation id for session-scoped agents;
        ``None`` for template agents.
    """

    id: str
    created_at: int
    name: str
    bundle_location: str
    version: int = 1
    description: str | None = None
    updated_at: int | None = None
    session_id: str | None = None

    @property
    @abc.abstractmethod
    def category(self) -> AgentCategory:
        """The tier this automation belongs to."""
        ...


@dataclass
class SystemAgent(Automation):
    """A platform-shipped, tightly-controlled system agent (satisfies :class:`AgentRole`)."""

    @property
    def category(self) -> AgentCategory:
        return "system"


@dataclass
class Workflow(Automation):
    """An orchestrator-definition automation (satisfies :class:`WorkflowRole`).

    Orchestrator accessors (specialists/department) are a step-2 seam: the API
    already derives org metadata from the loaded spec, so the entity does not
    duplicate it yet.
    """

    @property
    def category(self) -> AgentCategory:
        return "workflow"
