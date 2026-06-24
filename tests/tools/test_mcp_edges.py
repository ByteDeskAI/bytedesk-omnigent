"""Edge-path coverage for :mod:`omnigent.tools.mcp` helpers and guards."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import ElicitResult

from omnigent.spec.types import MCPOAuthConfig, MCPServerConfig
from omnigent.tools.mcp import (
    McpServerConnection,
    _resolve_databricks_token,
    _resolve_oauth_token,
    _resolve_token_exchange_token,
    _sleep,
)


def _http_config() -> MCPServerConfig:
    return MCPServerConfig(name="edge-server", url="http://localhost:9000/mcp")


def test_resolve_databricks_token_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "databricks.sdk":
            raise ImportError("no sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(ImportError, match="databricks-sdk is required"):
        _resolve_databricks_token("prod")


def test_resolve_databricks_token_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeConfig:
        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "Bearer dbx-token"}

    class _FakeClient:
        def __init__(self, profile: str) -> None:
            self.config = _FakeConfig()

    fake_mod = MagicMock()
    fake_mod.WorkspaceClient = _FakeClient
    monkeypatch.setitem(__import__("sys").modules, "databricks.sdk", fake_mod)
    assert _resolve_databricks_token("prod") == "dbx-token"


def test_resolve_databricks_token_returns_raw_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeConfig:
        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "raw-token-without-bearer-prefix"}

    class _FakeClient:
        def __init__(self, profile: str) -> None:
            self.config = _FakeConfig()

    fake_mod = MagicMock()
    fake_mod.WorkspaceClient = _FakeClient
    monkeypatch.setitem(__import__("sys").modules, "databricks.sdk", fake_mod)
    assert _resolve_databricks_token("prod") == "raw-token-without-bearer-prefix"


def test_resolve_databricks_token_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomClient:
        def __init__(self, profile: str) -> None:
            raise RuntimeError("bad profile")

    fake_mod = MagicMock()
    fake_mod.WorkspaceClient = _BoomClient
    monkeypatch.setitem(__import__("sys").modules, "databricks.sdk", fake_mod)
    with pytest.raises(RuntimeError, match="Failed to resolve Databricks token"):
        _resolve_databricks_token("missing")


def test_resolve_oauth_token_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.tools.mcp as mcp_mod

    mcp_mod._oauth_token_cache.clear()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    import httpx

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(RuntimeError, match="Failed to mint OAuth"):
        _resolve_oauth_token(MCPOAuthConfig(token_url="http://t", client_id="c"))


def test_resolve_oauth_token_invalid_expires_in_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.tools.mcp as mcp_mod

    mcp_mod._oauth_token_cache.clear()

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"access_token": "tok", "expires_in": "not-a-number"}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    assert _resolve_oauth_token(MCPOAuthConfig(token_url="http://t", client_id="c")) == "tok"


def _token_exchange_oauth() -> MCPOAuthConfig:
    return MCPOAuthConfig(
        token_url="https://identity.example.com/connect/token",
        client_id="agent-client",
        client_secret="agent-secret",
        scopes=["mcp.invoke"],
        resource="urn:bytedesk:mcp",
    )


def test_resolve_token_exchange_token_returns_cached_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.tools.mcp as mcp_mod
    import time

    oauth = _token_exchange_oauth()
    key = (oauth.token_url, oauth.client_id, "user-tok", oauth.resource, tuple(oauth.scopes))
    mcp_mod._token_exchange_cache.clear()
    mcp_mod._token_exchange_cache[key] = ("cached-obo", time.time() + 600)

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http")))
    assert _resolve_token_exchange_token(oauth, "user-tok") == "cached-obo"


def test_resolve_token_exchange_token_posts_exchange_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.tools.mcp as mcp_mod

    mcp_mod._token_exchange_cache.clear()
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"access_token": "obo-tok", "expires_in": 600}

    import httpx

    def _fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "post", _fake_post)
    token = _resolve_token_exchange_token(_token_exchange_oauth(), "user-access-tok")
    assert token == "obo-tok"
    form = captured["data"]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert form["subject_token"] == "user-access-tok"
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert form["client_secret"] == "agent-secret"
    assert form["scope"] == "mcp.invoke"
    assert form["resource"] == "urn:bytedesk:mcp"


def test_resolve_token_exchange_token_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.tools.mcp as mcp_mod

    mcp_mod._token_exchange_cache.clear()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    import httpx

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(RuntimeError, match="Failed to mint OAuth token-exchange token"):
        _resolve_token_exchange_token(_token_exchange_oauth(), "user-tok")


def test_resolve_token_exchange_token_missing_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.tools.mcp as mcp_mod

    mcp_mod._token_exchange_cache.clear()

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"token_type": "Bearer"}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError, match="returned no access_token"):
        _resolve_token_exchange_token(_token_exchange_oauth(), "user-tok")


def test_resolve_token_exchange_token_invalid_expires_in_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.tools.mcp as mcp_mod

    mcp_mod._token_exchange_cache.clear()

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"access_token": "obo-tok", "expires_in": "not-a-number"}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    assert _resolve_token_exchange_token(_token_exchange_oauth(), "user-tok") == "obo-tok"


@pytest.mark.asyncio
async def test_invoke_tool_requires_connected_session() -> None:
    conn = McpServerConnection(_http_config())
    with pytest.raises(RuntimeError, match="not initialized"):
        await conn._invoke_tool("demo", {})


@pytest.mark.asyncio
async def test_call_tool_with_elicitation_requires_connected_session() -> None:
    conn = McpServerConnection(_http_config())
    with pytest.raises(RuntimeError, match="not initialized"):
        await conn.call_tool_with_elicitation("demo", {})


@pytest.mark.asyncio
async def test_discover_or_use_cache_requires_session_when_cache_miss() -> None:
    conn = McpServerConnection(_http_config())
    conn._session = None
    with patch.object(conn, "_check_cache", return_value=None):
        with pytest.raises(RuntimeError, match="not initialized"):
            await conn._discover_or_use_cache()


@pytest.mark.asyncio
async def test_open_http_transport_requires_url() -> None:
    conn = McpServerConnection(MCPServerConfig(name="bad", url=None))
    stack = AsyncExitStack()
    with pytest.raises(RuntimeError, match="url is None"):
        await conn._open_http_transport(stack)


@pytest.mark.asyncio
async def test_open_stdio_transport_requires_command() -> None:
    conn = McpServerConnection(
        MCPServerConfig(name="bad", transport="stdio", command=None)
    )
    stack = AsyncExitStack()
    with pytest.raises(RuntimeError, match="command is None"):
        await conn._open_stdio_transport(stack)


@pytest.mark.asyncio
async def test_elicitation_handler_declines_without_session_or_callback() -> None:
    conn = McpServerConnection(_http_config())
    conn._active_session_id = None
    conn.elicitation_callback = None
    params = MagicMock()
    result = await conn._elicitation_handler(None, params)
    assert isinstance(result, ElicitResult)
    assert result.action == "decline"


@pytest.mark.asyncio
async def test_reconnect_closes_and_waits_for_new_lifecycle() -> None:
    conn = McpServerConnection(_http_config())
    loop = asyncio.get_running_loop()
    conn._ready_future = loop.create_future()
    conn._ready_future.set_result([])
    conn._close_event = asyncio.Event()
    conn._lifecycle_task = MagicMock()

    async def _fake_lifecycle() -> None:
        conn._ready_future.set_result([])

    with patch.object(conn, "close", new_callable=AsyncMock) as mock_close:
        with patch.object(conn, "_run_lifecycle", side_effect=_fake_lifecycle):
            await conn._reconnect()

    mock_close.assert_awaited_once()
    assert conn._ready_future.done()


@pytest.mark.asyncio
async def test_lifecycle_propagates_startup_failure_to_ready_future() -> None:
    conn = McpServerConnection(_http_config())
    loop = asyncio.get_running_loop()
    conn._ready_future = loop.create_future()
    conn._close_event = asyncio.Event()

    async def _boom_transport(_stack: AsyncExitStack) -> tuple[MagicMock, MagicMock]:
        raise RuntimeError("transport failed")

    with patch.object(conn, "_open_transport", side_effect=_boom_transport):
        await conn._run_lifecycle()

    with pytest.raises(RuntimeError, match="transport failed"):
        await conn._ready_future


@pytest.mark.asyncio
async def test_elicitation_handler_delegates_to_callback() -> None:
    conn = McpServerConnection(_http_config())
    conn._active_session_id = "conv_123"
    expected = ElicitResult(action="accept")

    async def _callback(session_id: str, params: Any) -> ElicitResult:
        assert session_id == "conv_123"
        return expected

    conn.elicitation_callback = _callback
    result = await conn._elicitation_handler(None, MagicMock())
    assert result is expected


@pytest.mark.asyncio
async def test_lifecycle_logs_steady_state_failure(caplog: pytest.LogCaptureFixture) -> None:
    conn = McpServerConnection(_http_config())
    loop = asyncio.get_running_loop()
    conn._ready_future = loop.create_future()
    conn._close_event = asyncio.Event()

    class _FakeSession:
        async def initialize(self) -> None:
            return None

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def _fake_open_transport(_stack: AsyncExitStack) -> tuple[MagicMock, MagicMock]:
        return MagicMock(), MagicMock()

    async def _boom_wait() -> None:
        raise RuntimeError("steady-state boom")

    with caplog.at_level("ERROR"):
        with patch.object(conn, "_open_transport", side_effect=_fake_open_transport):
            with patch("omnigent.tools.mcp.ClientSession", return_value=_FakeSession()):
                with patch.object(conn, "_discover_or_use_cache", return_value=[]):
                    conn._close_event.wait = _boom_wait  # type: ignore[method-assign]
                    await conn._run_lifecycle()

    assert "lifecycle task failed during steady state" in caplog.text


@pytest.mark.asyncio
async def test_sleep_awaits_real_delay() -> None:
    with patch("omnigent.tools.mcp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await _sleep(0.25)
    mock_sleep.assert_awaited_once_with(0.25)