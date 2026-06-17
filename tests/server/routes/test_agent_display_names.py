"""Unit tests for ``_agent_display_names_for`` (BDP-2161).

The sessions list + session-agent endpoints resolve the human display name
(``params.displayName``) at read time so a session-bound agent (e.g. Maya)
renders by name, not the slug. This pins the resolver: it reads displayName when
present, omits agents without one (client falls back to the slug), tolerates a
missing row / a spec that fails to load, and returns an empty map when no cache
is wired — never raising.
"""

from __future__ import annotations

import types

from omnigent.server.routes.sessions import _agent_display_names_for


class _Agent:
    def __init__(self, aid, *, bundle_location="loc", session_id=None):
        self.id = aid
        self.bundle_location = bundle_location
        self.session_id = session_id


class _Store:
    def __init__(self, agents):
        self._agents = agents

    def get(self, agent_id):
        return self._agents.get(agent_id)


class _Cache:
    """Returns a loaded spec whose params come from ``params_by_id``; raising
    ids simulate a spec that fails to load."""

    def __init__(self, params_by_id, *, raising=()):
        self._params = params_by_id
        self._raising = set(raising)

    def load(self, agent_id, bundle_location, *, expand_env):
        if agent_id in self._raising:
            raise RuntimeError("boom")
        spec = types.SimpleNamespace(params=self._params.get(agent_id))
        return types.SimpleNamespace(spec=spec)


def test_resolves_display_name_when_present():
    store = _Store({"a": _Agent("a"), "b": _Agent("b")})
    cache = _Cache({"a": {"displayName": "Maya Chen"}, "b": {"displayName": "Priya Nair"}})
    out = _agent_display_names_for(["a", "b"], store, cache)
    assert out == {"a": "Maya Chen", "b": "Priya Nair"}


def test_omits_agents_without_display_name():
    store = _Store({"a": _Agent("a"), "b": _Agent("b")})
    # b has params but no displayName; client falls back to the slug.
    cache = _Cache({"a": {"displayName": "Maya Chen"}, "b": {"title": "x"}})
    out = _agent_display_names_for(["a", "b"], store, cache)
    assert out == {"a": "Maya Chen"}


def test_tolerates_missing_row_and_bad_spec():
    store = _Store({"a": _Agent("a"), "c": _Agent("c")})  # "b" missing
    cache = _Cache({"a": {"displayName": "Maya Chen"}}, raising=("c",))
    out = _agent_display_names_for(["a", "b", "c"], store, cache)
    assert out == {"a": "Maya Chen"}


def test_none_params_is_safe():
    store = _Store({"a": _Agent("a")})
    cache = _Cache({"a": None})
    assert _agent_display_names_for(["a"], store, cache) == {}


def test_no_cache_returns_empty():
    store = _Store({"a": _Agent("a")})
    assert _agent_display_names_for(["a"], store, None) == {}


def test_expand_env_follows_session_scope():
    """Template agents (session_id None) load with expand_env=True; session
    copies with expand_env=False — mirrors _to_agent_object."""
    seen = {}

    class _RecordingCache:
        def load(self, agent_id, bundle_location, *, expand_env):
            seen[agent_id] = expand_env
            return types.SimpleNamespace(
                spec=types.SimpleNamespace(params={"displayName": "X"})
            )

    store = _Store({"tmpl": _Agent("tmpl", session_id=None), "sess": _Agent("sess", session_id="conv_1")})
    _agent_display_names_for(["tmpl", "sess"], store, _RecordingCache())
    assert seen == {"tmpl": True, "sess": False}
