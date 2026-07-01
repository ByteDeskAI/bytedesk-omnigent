"""Combined abstraction-spine flag interaction (BDP-2343, ADR-0145).

BDP-2343 ships all five spine flags ON together in production. ``scripts/test_parity_combined.sh``
diffs the full slice OFF vs ON; this module pins the one flag *interaction* that
matters at the dispatch seam so it is guarded in the normal pytest run too (no
shell harness required).

The interaction: ``execute_tool`` checks ``OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY``
(Phase 5) BEFORE ``OMNIGENT_USE_TOOL_EXECUTION_CONTEXT`` (Phase 4). So when BOTH
are on — the production config — the registry seam wins and
``_execute_tool_from_context`` is structurally unreachable through the integration
path. That is intended precedence, not a regression: the two seams unpack into the
SAME per-kwarg dispatch chain, so the routing result is identical either way. The
Phase-4 module's own unit tests still pass standalone (only the Phase-4 flag set),
which the second test below reproduces.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner import tool_dispatcher_registry as reg
from omnigent.runner.tool_execution_context import ToolExecutionContext


@pytest.mark.asyncio
async def test_registry_wins_when_both_tool_flags_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both tool flags on (production) → the registry seam wins; context path is skipped."""
    monkeypatch.setenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", "1")
    monkeypatch.setenv("OMNIGENT_USE_TOOL_EXECUTION_CONTEXT", "1")

    registry_calls: dict[str, ToolExecutionContext] = {}
    context_calls = {"n": 0}

    async def _fake_registry(ctx: ToolExecutionContext) -> str:
        registry_calls["ctx"] = ctx
        return "registry"

    async def _fake_context(ctx: ToolExecutionContext) -> str:
        context_calls["n"] += 1
        return "context"

    monkeypatch.setattr(reg, "dispatch_via_registry", _fake_registry)
    monkeypatch.setattr(tool_dispatch, "_execute_tool_from_context", _fake_context)

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    out = await tool_dispatch.execute_tool(
        tool_name="sys_read_inbox", arguments="{}", session_inbox=inbox
    )

    # Registry precedence: the Phase-5 seam handled dispatch...
    assert out == "registry"
    # ...carrying the same inbox by reference (the invariant both seams preserve)...
    assert registry_calls["ctx"].session_inbox is inbox
    # ...and the Phase-4 context path was never reached (structurally unreachable).
    assert context_calls["n"] == 0


@pytest.mark.asyncio
async def test_context_path_reached_when_only_phase4_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone Phase-4 config (registry flag off) still reaches the context seam.

    This is the invariant ``tests/runner/test_tool_execution_context.py`` relies on;
    the combined-flags parity harness deselects its flag-on routing test precisely
    because the production config (registry also on) supersedes this path.
    """
    monkeypatch.delenv("OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY", raising=False)
    monkeypatch.setenv("OMNIGENT_USE_TOOL_EXECUTION_CONTEXT", "1")

    async def _fake_context(ctx: ToolExecutionContext) -> str:
        return "context"

    async def _fake_registry(ctx: ToolExecutionContext) -> str:  # pragma: no cover
        raise AssertionError("registry seam must not run with its flag off")

    monkeypatch.setattr(tool_dispatch, "_execute_tool_from_context", _fake_context)
    monkeypatch.setattr(reg, "dispatch_via_registry", _fake_registry)

    out = await tool_dispatch.execute_tool(tool_name="sys_read_inbox", arguments="{}")
    assert out == "context"
