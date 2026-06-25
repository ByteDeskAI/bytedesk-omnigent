"""Three-tier memory access resolver (BDP-2458, amends ADR-0132/0133/0136).

This module IS the security mechanism for agent keyed memory — deliberately
small. Given the address/scope a tool was called with **and the server-verified
caller identity** (agent id + department, both derived server-side from the
session's agent, never the model), it returns the compartment target to use or an
:class:`AccessDenied`. The owner is always stamped from the verified identity, so
an agent can neither read another agent's private memory nor a department it does
not belong to.

Three tiers (the founder's rules):

* ``org:<key>``          — every agent may read/write (the org blackboard).
* ``dept:<dept>:<key>``  — only agents whose department == ``<dept>``.
* ``agent:<key>``        — private to the calling agent (owner = caller id).

Departments are free-form strings on the agent bundle (``spec.params.department``,
e.g. ``"People Operations"``), so both sides are slug-normalized before matching
and for the compartment name — ``"People Operations"`` and ``"people-operations"``
are the same department.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NON_SLUG = re.compile(r"[^a-z0-9]+")

# Constant shared owners (mirror routes/memory.py + memory.py:_resolve_owner).
_TEAM_OWNER = "team"
_TOPIC_OWNER = "shared"
_ORG_NAME = "org-context"
#: The single private keyed compartment per agent (scope=agent, owner=<caller>).
_AGENT_DEFAULT_NAME = "default"


def _slug(value: str) -> str:
    """Normalize a free-form department/label to a stable slug for matching."""
    return _NON_SLUG.sub("-", value.strip().lower()).strip("-")


@dataclass(frozen=True)
class MemoryTarget:
    """A resolved, access-granted compartment coordinate.

    :param scope: store scope — ``team`` / ``topic`` / ``agent``.
    :param owner: server-stamped owner (constant for shared tiers, the verified
        caller id for the private agent tier).
    :param name: compartment name.
    :param key: addressable slot key, or ``None`` for ambient (search/append).
    """

    scope: str
    owner: str
    name: str
    key: str | None


@dataclass(frozen=True)
class AccessDenied:
    """A denied access decision carrying a human-readable reason."""

    reason: str


Resolution = MemoryTarget | AccessDenied


def _authorize_dept(
    dept_token: str, *, caller_department: str | None, key: str | None
) -> Resolution:
    """Grant a department compartment only to a member of that department."""
    want = _slug(dept_token)
    if not want:
        return AccessDenied(reason="department address must name a department")
    if caller_department is None:
        return AccessDenied(
            reason=f"dept:{want} is restricted to its members; caller has no department"
        )
    if _slug(caller_department) != want:
        return AccessDenied(
            reason=(
                f"dept:{want} is restricted to its members; caller is in "
                f"'{caller_department}'"
            )
        )
    return MemoryTarget(scope="topic", owner=_TOPIC_OWNER, name=f"dept:{want}", key=key)


def _agent_target(key: str | None, *, caller_agent_id: str | None) -> Resolution:
    """Private agent compartment — owner is always the verified caller id."""
    if not caller_agent_id:
        return AccessDenied(reason="agent-scope memory requires a verified caller identity")
    return MemoryTarget(
        scope="agent", owner=caller_agent_id, name=_AGENT_DEFAULT_NAME, key=key
    )


def resolve_address(
    address: str, *, caller_agent_id: str | None, caller_department: str | None
) -> Resolution:
    """Resolve a keyed address (``org:<key>`` / ``dept:<d>:<key>`` / ``agent:<key>``)."""
    raw = (address or "").strip()
    if not raw:
        return AccessDenied(reason="address is required (e.g. 'org:charter')")
    parts = raw.split(":")
    cls = parts[0]
    if cls == "org":
        if len(parts) != 2 or not parts[1]:
            return AccessDenied(reason="org address must be 'org:<key>'")
        return MemoryTarget(scope="team", owner=_TEAM_OWNER, name=_ORG_NAME, key=parts[1])
    if cls == "dept":
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return AccessDenied(reason="dept address must be 'dept:<dept>:<key>'")
        return _authorize_dept(parts[1], caller_department=caller_department, key=parts[2])
    if cls == "agent":
        # agent:<key> (self) or agent:<id>:<key> (explicit id, must equal caller).
        if len(parts) == 2 and parts[1]:
            return _agent_target(parts[1], caller_agent_id=caller_agent_id)
        if len(parts) == 3 and parts[1] and parts[2]:
            if not caller_agent_id:
                return AccessDenied(
                    reason="agent-scope memory requires a verified caller identity"
                )
            if parts[1] != caller_agent_id:
                return AccessDenied(
                    reason="agent-scope memory is private to its owner; "
                    "cannot address another agent"
                )
            return _agent_target(parts[2], caller_agent_id=caller_agent_id)
        return AccessDenied(reason="agent address must be 'agent:<key>'")
    return AccessDenied(
        reason=f"unknown address class {cls!r}; expected 'org:', 'dept:', or 'agent:'"
    )


def resolve_prefix(
    prefix: str, *, caller_agent_id: str | None, caller_department: str | None
) -> Resolution:
    """Resolve a browse prefix (``org`` / ``dept:<dept>`` / ``agent``) for list."""
    raw = (prefix or "").strip()
    if raw == "org":
        return MemoryTarget(scope="team", owner=_TEAM_OWNER, name=_ORG_NAME, key=None)
    if raw.startswith("dept:"):
        return _authorize_dept(raw[len("dept:") :], caller_department=caller_department, key=None)
    if raw == "agent" or raw == "agent:":
        return _agent_target(None, caller_agent_id=caller_agent_id)
    return AccessDenied(reason=f"unknown list prefix {raw!r}; expected 'org' or 'dept:<dept>'")


def resolve_scope_name(
    scope: str, name: str, *, caller_agent_id: str | None, caller_department: str | None
) -> Resolution:
    """Resolve the ambient (search/append) ``scope`` + ``name`` form.

    ``team`` = org (allow all); ``topic`` name ``dept:<d>`` = department (members
    only), ``initiative:<id>`` / other = shared topic (allow all); ``agent`` =
    private to the caller.
    """
    if scope == "team":
        return MemoryTarget(scope="team", owner=_TEAM_OWNER, name=name or _ORG_NAME, key=None)
    if scope == "topic":
        nm = (name or "").strip()
        if nm.startswith("dept:"):
            return _authorize_dept(
                nm[len("dept:") :], caller_department=caller_department, key=None
            )
        # initiative:<id> and other named topics are org-wide shared spaces.
        return MemoryTarget(scope="topic", owner=_TOPIC_OWNER, name=nm or "shared", key=None)
    if scope == "agent":
        if not caller_agent_id:
            return AccessDenied(reason="agent-scope memory requires a verified caller identity")
        return MemoryTarget(
            scope="agent", owner=caller_agent_id, name=name or _AGENT_DEFAULT_NAME, key=None
        )
    return AccessDenied(reason=f"unknown memory scope {scope!r}")
