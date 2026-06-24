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

    def _obo(oauth, subject_token, act_as=None):
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
        lambda oauth, subject_token, act_as=None: "obo-tok",
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
        def __init__(
            self, config, cwd=None, elicitation_callback=None, subject_token=None, agent_id=None
        ):
            self.config = config
            self.subject_token = subject_token
            self.agent_id = agent_id
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


# ── act_as=<agent_id>: stamp the SPECIFIC persona as act_sub (BDP-2435) ───────


def test_token_exchange_form_carries_act_as(monkeypatch):
    # When ``act_as`` is set, the token-exchange POST form must carry it so the
    # platform stamps the SPECIFIC agent persona as ``act_sub``.
    import omnigent.tools.mcp as mcp

    mcp._token_exchange_cache.clear()
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "obo-tok", "expires_in": 600}

    def _fake_post(url, data=None, headers=None, timeout=None):
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)
    tok = mcp._resolve_token_exchange_token(_OAUTH, "user-access-tok", act_as="maya")
    assert tok == "obo-tok"
    assert captured["data"]["act_as"] == "maya"


def test_token_exchange_without_act_as_omits_form_field(monkeypatch):
    # Degrade-to-default: no ``act_as`` ⇒ no ``act_as`` key in the form (the
    # platform falls back to the shared service-account ``act_sub``).
    import omnigent.tools.mcp as mcp

    mcp._token_exchange_cache.clear()
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "obo-tok", "expires_in": 600}

    def _fake_post(url, data=None, headers=None, timeout=None):
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)
    mcp._resolve_token_exchange_token(_OAUTH, "user-access-tok")
    assert "act_as" not in captured["data"]


def test_token_exchange_cache_distinguishes_act_as(monkeypatch):
    # Two different ``act_as`` values for the SAME subject_token must mint two
    # distinct tokens — the cache key folds ``act_as`` in, so they cannot share
    # a cache hit (else two agents would get the wrong act_sub).
    import omnigent.tools.mcp as mcp

    mcp._token_exchange_cache.clear()
    minted: list[str] = []
    counter = {"n": 0}

    class _Resp:
        def __init__(self, tok):
            self._tok = tok

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": self._tok, "expires_in": 600}

    def _fake_post(url, data=None, headers=None, timeout=None):
        counter["n"] += 1
        tok = f"obo-{counter['n']}"
        minted.append(data.get("act_as"))
        return _Resp(tok)

    monkeypatch.setattr("httpx.post", _fake_post)
    t1 = mcp._resolve_token_exchange_token(_OAUTH, "user-access-tok", act_as="maya")
    t2 = mcp._resolve_token_exchange_token(_OAUTH, "user-access-tok", act_as="elias")
    # Same act_as re-hits the cache (no third mint).
    t1b = mcp._resolve_token_exchange_token(_OAUTH, "user-access-tok", act_as="maya")
    assert t1 != t2
    assert t1b == t1
    assert minted == ["maya", "elias"]  # exactly two mints, not three


def test_connection_passes_agent_id_as_act_as(monkeypatch):
    # ``McpServerConnection`` carrying ``agent_id`` threads it into the
    # token-exchange call as ``act_as``.
    captured: dict[str, object] = {}

    def _obo(oauth, subject_token, act_as=None):
        captured["act_as"] = act_as
        captured["subject_token"] = subject_token
        return "obo-tok"

    monkeypatch.setattr("omnigent.tools.mcp._resolve_token_exchange_token", _obo)
    conn = McpServerConnection(
        config=_http_cfg(oauth=_OAUTH),
        subject_token="user-access-tok",
        agent_id="maya",
    )
    headers = conn._resolve_http_headers()
    assert headers == {"Authorization": "Bearer obo-tok"}
    assert captured["act_as"] == "maya"
    assert captured["subject_token"] == "user-access-tok"


def test_connection_without_agent_id_passes_none_act_as(monkeypatch):
    # Degrade-to-default: no agent_id ⇒ act_as=None into the mint (shared act_sub).
    captured: dict[str, object] = {}

    def _obo(oauth, subject_token, act_as=None):
        captured["act_as"] = act_as
        return "obo-tok"

    monkeypatch.setattr("omnigent.tools.mcp._resolve_token_exchange_token", _obo)
    conn = McpServerConnection(config=_http_cfg(oauth=_OAUTH), subject_token="user-access-tok")
    conn._resolve_http_headers()
    assert captured["act_as"] is None


# ── pool re-key folds agent_id (two agents, same subject ⇒ two connections) ───


def test_obo_pool_key_distinguishes_agents():
    # Same subject_token + different agent_id ⇒ different pool keys, so two agents
    # acting for the SAME user never share a pooled connection/bearer.
    from omnigent.runner.mcp_manager import _obo_pool_key

    k_maya = _obo_pool_key("spec1", "user-access-tok", "maya")
    k_elias = _obo_pool_key("spec1", "user-access-tok", "elias")
    k_no_agent = _obo_pool_key("spec1", "user-access-tok", None)
    assert k_maya != k_elias
    assert k_maya != k_no_agent
    assert k_elias != k_no_agent
    # Absent subject_token ⇒ bare spec_hash, unchanged (degrade).
    assert _obo_pool_key("spec1", None, "maya") == "spec1"
    assert _obo_pool_key("spec1", None, None) == "spec1"


@pytest.mark.asyncio
async def test_call_tool_threads_agent_id_to_connection(monkeypatch):
    """An OBO ``call_tool`` with ``agent_id`` connects a connection carrying that
    agent_id; two agents for the same subject_token get DISTINCT connections."""
    from mcp.types import Tool as McpToolDef

    from omnigent.runner.mcp_manager import RunnerMcpManager

    created: list[object] = []

    class _FakeConn:
        def __init__(
            self, config, cwd=None, elicitation_callback=None, subject_token=None, agent_id=None
        ):
            self.config = config
            self.subject_token = subject_token
            self.agent_id = agent_id
            self.tools = [McpToolDef(name="ping", inputSchema={"type": "object"})]
            created.append(self)

        async def connect(self):
            return self.tools

        async def call_tool(self, name, arguments, session_id=None):
            return f"ok:{name}:agent={self.agent_id}"

        async def close(self):
            return None

    monkeypatch.setattr("omnigent.runner.mcp_manager.McpServerConnection", _FakeConn)

    mgr = RunnerMcpManager()
    cfg = _http_cfg(oauth=_OAUTH, tool_allowlist=["ping"])
    spec = _make_spec([cfg])

    out_maya = await mgr.call_tool(
        spec,
        "bytedesk-mcp__ping",
        {},
        session_id="conv_1",
        subject_token="user-access-tok",
        agent_id="maya",
    )
    out_elias = await mgr.call_tool(
        spec,
        "bytedesk-mcp__ping",
        {},
        session_id="conv_1",
        subject_token="user-access-tok",
        agent_id="elias",
    )
    assert out_maya == "ok:ping:agent=maya"
    assert out_elias == "ok:ping:agent=elias"

    # Two distinct OBO connections (one per agent), proving the pool key folds
    # agent_id in — same subject_token did NOT collapse them onto one bearer.
    agent_ids = sorted(str(c.agent_id) for c in created)
    assert agent_ids == ["elias", "maya"]
