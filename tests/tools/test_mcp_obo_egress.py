"""ByteDesk.Mcp OBO egress: present a token-exchange bearer (BDP-2434 Part 3, Option A).

When a ByteDesk.Mcp tool call runs with an acting identity carrying a
``subject_token``, the MCP connection must present an on-behalf-of bearer minted
via RFC 8693 token-exchange instead of the ``client_credentials`` bearer. Absent
``subject_token`` ⇒ today's ``client_credentials`` egress, byte-identical.

Two seams under test:

1. ``McpServerConnection._resolve_http_headers`` prefers a token-exchange bearer
   when the connection carries a ``subject_token`` and the server config has an
   ``oauth`` block; otherwise it is byte-identical to today.
2. ``RunnerMcpManager.call_tool(subject_token=...)`` routes an OBO-active call to
   a connection keyed separately from the shared (no-OBO) pool entry, so an OBO
   session never reuses (or poisons) the shared ``client_credentials`` connection.
"""

from __future__ import annotations

import pytest

from omnigent.spec.types import MCPOAuthConfig, MCPServerConfig
from omnigent.tools.mcp import McpServerConnection

_OAUTH = MCPOAuthConfig(
    token_url="https://identity.bytedesk.ai/connect/token",
    client_id="agent-client",
    client_secret="agent-secret",
    scopes=["mcp.invoke"],
    resource="urn:bytedesk:mcp",
)


def _http_cfg(**kw) -> MCPServerConfig:
    return MCPServerConfig(name="bytedesk-mcp", transport="http", url="https://mcp/mcp", **kw)


# ── connection header resolution: OBO vs client_credentials ──────────────────


def test_connection_without_subject_token_uses_client_credentials(monkeypatch):
    # Degrade-to-default: no subject_token ⇒ the existing OAuth client-credentials
    # bearer, byte-identical to today's egress.
    monkeypatch.setattr("omnigent.tools.mcp._resolve_oauth_token", lambda oauth: "cc-tok")
    conn = McpServerConnection(config=_http_cfg(oauth=_OAUTH))
    headers = conn._resolve_http_headers()
    assert headers == {"Authorization": "Bearer cc-tok"}


def test_connection_with_subject_token_uses_token_exchange(monkeypatch):
    # OBO active: present the token-exchange bearer, NOT the client-credentials one.
    monkeypatch.setattr(
        "omnigent.tools.mcp._resolve_oauth_token",
        lambda oauth: pytest.fail("client_credentials must not be used when OBO is active"),
    )
    captured: dict[str, object] = {}

    def _obo(oauth, subject_token):
        captured["oauth"] = oauth
        captured["subject_token"] = subject_token
        return "obo-tok"

    monkeypatch.setattr("omnigent.tools.mcp._resolve_token_exchange_token", _obo)
    conn = McpServerConnection(config=_http_cfg(oauth=_OAUTH), subject_token="user-access-tok")
    headers = conn._resolve_http_headers()
    assert headers == {"Authorization": "Bearer obo-tok"}
    assert captured["subject_token"] == "user-access-tok"
    assert captured["oauth"] is _OAUTH


def test_connection_subject_token_without_oauth_is_unchanged(monkeypatch):
    # A server with no oauth block can't do token-exchange; a stray subject_token
    # must not invent an Authorization header (degrade-to-default).
    conn = McpServerConnection(config=_http_cfg(), subject_token="user-access-tok")
    assert conn._resolve_http_headers() is None


def test_connection_explicit_header_still_wins_over_obo(monkeypatch):
    # An explicit Authorization header is the highest-precedence source; OBO must
    # not override an operator-pinned credential.
    monkeypatch.setattr(
        "omnigent.tools.mcp._resolve_token_exchange_token",
        lambda oauth, subject_token: "obo-tok",
    )
    conn = McpServerConnection(
        config=_http_cfg(oauth=_OAUTH, headers={"Authorization": "Bearer pinned"}),
        subject_token="user-access-tok",
    )
    assert conn._resolve_http_headers() == {"Authorization": "Bearer pinned"}


# ── pool re-keying: OBO call routes to its own connection ────────────────────


def _make_spec(configs):
    class _Spec:
        name = "agent"
        mcp_servers = configs

    return _Spec()


@pytest.mark.asyncio
async def test_call_tool_obo_routes_to_subject_token_connection(monkeypatch):
    """An OBO ``call_tool`` connects a SEPARATE connection carrying the
    subject_token; the no-OBO call uses the shared (no-subject_token) one."""
    from mcp.types import Tool as McpToolDef

    from omnigent.runner.mcp_manager import RunnerMcpManager

    created: list[McpServerConnection] = []

    class _FakeConn:
        def __init__(self, config, cwd=None, elicitation_callback=None, subject_token=None):
            self.config = config
            self.subject_token = subject_token
            self.tools = [McpToolDef(name="ping", inputSchema={"type": "object"})]
            created.append(self)  # type: ignore[arg-type]

        async def connect(self):
            return self.tools

        async def call_tool(self, name, arguments, session_id=None):
            return f"ok:{name}:subj={self.subject_token}"

        async def close(self):
            return None

    monkeypatch.setattr("omnigent.runner.mcp_manager.McpServerConnection", _FakeConn)

    mgr = RunnerMcpManager()
    cfg = _http_cfg(oauth=_OAUTH, tool_allowlist=["ping"])
    spec = _make_spec([cfg])

    # No-OBO call: shared connection, subject_token None.
    out1 = await mgr.call_tool(spec, "bytedesk-mcp__ping", {}, session_id="conv_1")
    assert out1 == "ok:ping:subj=None"

    # OBO call: a distinct connection carrying the subject_token.
    out2 = await mgr.call_tool(
        spec, "bytedesk-mcp__ping", {}, session_id="conv_1", subject_token="user-access-tok"
    )
    assert out2 == "ok:ping:subj=user-access-tok"

    # Two distinct connections were created (shared + OBO), proving the pool
    # re-key — the OBO call never reused the shared client_credentials connection.
    subject_tokens = sorted(str(c.subject_token) for c in created)
    assert subject_tokens == ["None", "user-access-tok"]
