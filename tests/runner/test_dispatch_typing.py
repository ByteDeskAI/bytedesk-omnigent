"""Typing-contract tests for runner dispatch (sweep-2 BDP-2363).

These pin the structural contracts the dispatch typing depends on:

- ``AgentSpecLike`` (the import-free Protocol the dispatch sites read) is
  satisfied structurally by the real :class:`omnigent.spec.types.AgentSpec`.
- The ``ToolExecutionContext`` carrier's ``mcp_manager`` field accepts BOTH
  concrete backends (direct + proxy) because it is typed as the ``McpManager``
  seam Protocol, not a concrete class.

The first is verified at runtime by exercising the attribute surface the
Protocol declares; both halves are additionally pinned statically by ``mypy``
running over the runner modules (a renamed ``agent_spec`` attribute or a
concrete re-pin of ``mcp_manager`` then fails the type check, not just here).
"""

from __future__ import annotations

import httpx

from omnigent.runner.mcp_manager import McpManager, RunnerMcpManager
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.tool_dispatch import AgentSpecLike
from omnigent.runner.tool_execution_context import ToolExecutionContext
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _accepts_agent_spec_like(spec: AgentSpecLike) -> str | None:
    """Typed sink: only a structural ``AgentSpecLike`` is assignable here."""
    return spec.name


def test_agent_spec_is_structural_agent_spec_like() -> None:
    """The real AgentSpec satisfies AgentSpecLike (structural acceptance)."""
    spec = AgentSpec(
        spec_version=1,
        name="typing-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "omnigent"}),
    )
    # Assignable to the AgentSpecLike-typed sink + carries the read surface.
    assert _accepts_agent_spec_like(spec) == "typing-agent"
    for attr in (
        "name",
        "tools",
        "skills",
        "skills_filter",
        "mcp_servers",
        "local_tools",
        "sub_agents",
        "executor",
        "os_env",
    ):
        assert hasattr(spec, attr), f"AgentSpec missing AgentSpecLike attr {attr!r}"


def test_carrier_accepts_direct_mcp_manager_as_seam() -> None:
    """ToolExecutionContext.mcp_manager accepts the direct backend (seam)."""
    manager: McpManager = RunnerMcpManager()
    ctx = ToolExecutionContext(tool_name="x", arguments="{}", mcp_manager=manager)
    assert ctx.mcp_manager is manager


def test_carrier_accepts_proxy_mcp_manager_as_seam() -> None:
    """ToolExecutionContext.mcp_manager accepts the proxy backend (seam).

    A concrete re-pin of the field would reject one of the two backends; both
    being assignable proves the field is typed as the ``McpManager`` Protocol.
    """
    manager: McpManager = ProxyMcpManager("conv_abc", httpx.AsyncClient())
    ctx = ToolExecutionContext(tool_name="x", arguments="{}", mcp_manager=manager)
    assert ctx.mcp_manager is manager
