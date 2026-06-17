"""OAuth client-credentials auth for HTTP MCP servers (BDP-2182).

Lets a headless agent reach an OAuth-protected MCP (e.g. ByteDesk.Mcp, an
OpenIddict resource server) by minting + caching + refreshing a bearer token
from a token endpoint — the provider-agnostic counterpart to the Databricks
profile path. Covers the parser (auth: {type: oauth}) and the runtime token
resolver + header injection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.tools.mcp as mcp_mod
from omnigent.spec.parser import parse
from omnigent.spec.types import MCPOAuthConfig, MCPServerConfig
from omnigent.tools.mcp import McpServerConnection, _resolve_oauth_token


# ---- parser ----

_BUNDLE = """\
spec_version: 1
name: oauth-mcp-agent
executor: {{type: omnigent, config: {{harness: claude-sdk}}}}
prompt: |
  test
tools:
  platform:
    type: mcp
    url: http://bytedesk-mcp:46463/mcp
    auth:
      type: oauth
      token_url: https://identity.bytedesk.ai/connect/token
      client_id: omnigent-mcp
      client_secret: {secret}
      scopes: [mcp.read, mcp.write]
      resource: bytedesk-mcp
"""


def _write_bundle(tmp_path: Path, secret: str = "s3cr3t") -> Path:
    d = tmp_path / "oauth-mcp-agent"
    d.mkdir()
    (d / "config.yaml").write_text(_BUNDLE.format(secret=secret))
    return d


def test_parser_builds_oauth_config(tmp_path):
    spec = parse(_write_bundle(tmp_path), expand_env=False)
    servers = [s for s in spec.mcp_servers if s.name == "platform"]
    assert servers, [s.name for s in spec.mcp_servers]
    o = servers[0].oauth
    assert isinstance(o, MCPOAuthConfig)
    assert o.token_url == "https://identity.bytedesk.ai/connect/token"
    assert o.client_id == "omnigent-mcp"
    assert o.client_secret == "s3cr3t"
    assert o.scopes == ["mcp.read", "mcp.write"]
    assert o.resource == "bytedesk-mcp"


def test_parser_expands_env_in_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("BD_MCP_SECRET", "from-env")
    spec = parse(_write_bundle(tmp_path, secret="${BD_MCP_SECRET}"), expand_env=True)
    o = [s for s in spec.mcp_servers if s.name == "platform"][0].oauth
    assert o.client_secret == "from-env"


def test_parser_requires_token_url_and_client_id(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "config.yaml").write_text(
        "spec_version: 1\nname: bad\nexecutor: {type: omnigent, config: {harness: claude-sdk}}\n"
        "prompt: |\n  x\ntools:\n  p:\n    type: mcp\n    url: http://x/mcp\n"
        "    auth: {type: oauth, client_id: only-id}\n"
    )
    with pytest.raises(Exception, match="token_url"):
        parse(d, expand_env=False)


def test_secret_redacted_in_repr():
    cfg = MCPServerConfig(
        name="p", url="http://x/mcp",
        oauth=MCPOAuthConfig(token_url="http://t", client_id="c", client_secret="TOPSECRET"),
    )
    assert "TOPSECRET" not in repr(cfg.oauth)


# ---- runtime token resolver ----

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def test_resolve_oauth_token_mints_and_caches(monkeypatch):
    mcp_mod._oauth_token_cache.clear()
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append((url, data))
        return _FakeResp({"access_token": "tok-1", "expires_in": 3600})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    o = MCPOAuthConfig(token_url="http://t/token", client_id="c", client_secret="s",
                       scopes=["a", "b"], resource="r")
    assert _resolve_oauth_token(o) == "tok-1"
    # second call within validity → cached, no new POST
    assert _resolve_oauth_token(o) == "tok-1"
    assert len(calls) == 1
    # form carried the grant + scope (space-joined) + resource
    _, data = calls[0]
    assert data["grant_type"] == "client_credentials"
    assert data["client_id"] == "c"
    assert data["scope"] == "a b"
    assert data["resource"] == "r"


def test_resolve_oauth_token_refreshes_when_expired(monkeypatch):
    mcp_mod._oauth_token_cache.clear()
    seq = iter(["tok-1", "tok-2"])

    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResp({"access_token": next(seq), "expires_in": 1})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    # freeze time so the first token is already inside the refresh skew window
    t = [1000.0]
    monkeypatch.setattr(mcp_mod.time, "time", lambda: t[0])

    o = MCPOAuthConfig(token_url="http://t/token", client_id="c")
    assert _resolve_oauth_token(o) == "tok-1"
    t[0] += 5  # past expires_in(1) + skew → must refresh
    assert _resolve_oauth_token(o) == "tok-2"


def test_resolve_oauth_token_errors_without_access_token(monkeypatch):
    mcp_mod._oauth_token_cache.clear()
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({"error": "nope"}))
    with pytest.raises(RuntimeError, match="no access_token"):
        _resolve_oauth_token(MCPOAuthConfig(token_url="http://t", client_id="c"))


def test_resolve_http_headers_injects_oauth_bearer(monkeypatch):
    mcp_mod._oauth_token_cache.clear()
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({"access_token": "tok-X", "expires_in": 3600}))
    cfg = MCPServerConfig(name="p", url="http://x/mcp",
                          oauth=MCPOAuthConfig(token_url="http://t", client_id="c"))
    conn = McpServerConnection(cfg)
    headers = conn._resolve_http_headers()
    assert headers["Authorization"] == "Bearer tok-X"


def test_explicit_authorization_header_wins_over_oauth(monkeypatch):
    mcp_mod._oauth_token_cache.clear()
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({"access_token": "tok-X", "expires_in": 3600}))
    cfg = MCPServerConfig(name="p", url="http://x/mcp",
                          headers={"Authorization": "Bearer explicit"},
                          oauth=MCPOAuthConfig(token_url="http://t", client_id="c"))
    conn = McpServerConnection(cfg)
    assert conn._resolve_http_headers()["Authorization"] == "Bearer explicit"
