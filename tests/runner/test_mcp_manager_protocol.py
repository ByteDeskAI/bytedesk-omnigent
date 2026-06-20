"""Tests for the :class:`McpManager` Protocol + factory (BDP-2348).

The runner dispatch sites pass ``mcp_manager`` around as a bare ``Any``, even
though it is always one of two concrete backends — the direct
:class:`RunnerMcpManager` or the proxy
:class:`ProxyMcpManager`. This formalizes that duck type as a
``runtime_checkable`` Protocol and a small factory; both concrete managers must
remain structural matches and the factory must pick the right one.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.runner.mcp_manager import (
    McpManager,
    RunnerMcpManager,
    make_mcp_manager,
)
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager


def test_runner_manager_is_structural_match() -> None:
    """DirectMcpManager (RunnerMcpManager) satisfies the McpManager Protocol."""
    manager = RunnerMcpManager()
    assert isinstance(manager, McpManager)


def test_proxy_manager_is_structural_match() -> None:
    """ProxyMcpManager satisfies the McpManager Protocol."""
    manager = ProxyMcpManager("conv_abc", httpx.AsyncClient())
    assert isinstance(manager, McpManager)


def test_factory_returns_direct_manager_without_session() -> None:
    """No session_id → direct RunnerMcpManager."""
    manager = make_mcp_manager()
    assert isinstance(manager, RunnerMcpManager)
    assert isinstance(manager, McpManager)


def test_factory_returns_proxy_manager_with_session() -> None:
    """A session_id + server_client → ProxyMcpManager."""
    manager = make_mcp_manager(
        session_id="conv_abc",
        server_client=httpx.AsyncClient(),
    )
    assert isinstance(manager, ProxyMcpManager)
    assert isinstance(manager, McpManager)


def test_factory_proxy_requires_server_client() -> None:
    """Proxy mode without a server_client is a hard error, not a silent fallback."""
    with pytest.raises(ValueError, match="server_client"):
        make_mcp_manager(session_id="conv_abc")
