"""BDP-2422 Phase 1a: acting_identity threads from execute_tool → ToolContext.

Proves the runner-side threading the principal-propagation slice depends on:
the representative local-python leaf stamps ctx.acting_identity, the absent
default is None (spawn-safe), and the shared execute_tool chokepoint forwards
the value to the leaf. The terminal/skill leaves take the identical additive
kwarg; the cross-boundary carrier is covered by the dispatch-path integration
test.
"""

from __future__ import annotations

import asyncio

from omnigent.identity.defaults import acting_identity_for
from omnigent.runner import tool_dispatch
from omnigent.server.principal import Principal


class _FakeManager:
    """Captures the ToolContext _execute_local_python_tool builds."""

    captured: object | None = None

    def __init__(self, spec, workdir=None):
        pass

    def call_tool(self, tool_name, args, ctx):
        _FakeManager.captured = ctx
        return "ok"


def _run_local_leaf(monkeypatch, *, acting_identity):
    _FakeManager.captured = None
    monkeypatch.setattr(tool_dispatch, "ToolManager", _FakeManager)
    out = asyncio.run(
        tool_dispatch._execute_local_python_tool(
            "some_tool",
            "{}",
            agent_spec=object(),  # non-None; FakeManager ignores it
            conversation_id="conv1",
            task_id="t1",
            agent_id="maya",
            runner_workspace=None,
            acting_identity=acting_identity,
        )
    )
    assert out == "ok"
    return _FakeManager.captured


def test_local_python_leaf_threads_acting_identity(monkeypatch):
    ident = acting_identity_for(Principal(user_id="alice@x", roles=("admin",)), agent_id="maya")
    ctx = _run_local_leaf(monkeypatch, acting_identity=ident)
    assert ctx.acting_identity is ident
    assert ctx.acting_identity.principal.user_id == "alice@x"


def test_local_python_leaf_absent_identity_is_none(monkeypatch):
    # Spawn-safe default: omitting the kwarg ⇒ ctx.acting_identity is None,
    # identical to today's behaviour.
    ctx = _run_local_leaf(monkeypatch, acting_identity=None)
    assert ctx.acting_identity is None


def test_execute_tool_forwards_acting_identity_to_leaf(monkeypatch):
    # The shared execute_tool chokepoint must pass the value to the leaf.
    captured = {}

    async def _fake_leaf(tool_name, arguments, *, acting_identity=None, **kw):
        captured["acting_identity"] = acting_identity
        return "ok"

    monkeypatch.setattr(tool_dispatch, "_execute_local_python_tool", _fake_leaf)
    monkeypatch.setattr(tool_dispatch, "_is_spec_local_python_tool", lambda *a, **k: True)

    ident = acting_identity_for(Principal(user_id="alice@x"), agent_id="maya")
    out = asyncio.run(
        tool_dispatch.execute_tool(
            tool_name="some_local_tool",
            arguments="{}",
            agent_spec=object(),
            conversation_id="conv1",
            task_id="t1",
            agent_id="maya",
            acting_identity=ident,
        )
    )
    assert out == "ok"
    assert captured["acting_identity"] is ident
