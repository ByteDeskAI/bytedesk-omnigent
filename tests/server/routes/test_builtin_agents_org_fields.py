"""GET /v1/agents org-field projection from bundle params (BDP-2149, ADR-0134).

``_to_agent_object`` derives managers/department/title from ``spec.params`` (the
same projection-time pattern as ``display_name``) so the org chart is a derived
view with no new entity field/column. Managers stay as raw objects; the
platform side flattens to manager-id slugs.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.entities import Agent, LoadedAgent
from omnigent.server.routes.builtin_agents import _to_agent_object
from omnigent.spec import parse

_AGENTS = Path(__file__).resolve().parents[3] / "deploy" / "bytedesk" / "agents"


class _FakeCache:
    def __init__(self, spec) -> None:
        self._spec = spec

    def load(self, agent_id: str, bundle_location: str, expand_env: bool = True) -> LoadedAgent:
        return LoadedAgent(spec=self._spec, workdir=Path("."))


class _BoomCache:
    def load(self, *args, **kwargs) -> LoadedAgent:
        raise RuntimeError("bundle unreadable")


def test_org_fields_projected_from_params() -> None:
    spec = parse(_AGENTS / "platform-developer", expand_env=False)
    agent = Agent(id="ag_x", created_at=0, name="platform-developer", bundle_location="x")
    obj = _to_agent_object(agent, _FakeCache(spec))
    assert obj.display_name == "Platform Developer"
    assert obj.title == "Platform Engineer"
    assert obj.department == "Engineering"
    assert any(m.get("id") == "chief-of-staff" for m in obj.managers), obj.managers


def test_org_fields_default_when_spec_unloadable() -> None:
    agent = Agent(id="ag_y", created_at=0, name="x", bundle_location="x")
    obj = _to_agent_object(agent, _BoomCache())
    assert obj.managers == []
    assert obj.department is None
    assert obj.title is None
