"""BDP-2422 Phase 1a-ii: mcp_execute decodes the signed header → execute_tool.

Path A is the runner entry point that HAS the inbound Request: the server
attaches a signed X-Omnigent-Acting-Identity header, mcp_execute verifies it and
threads the ActingIdentity into execute_tool. Absent/invalid header ⇒ None ⇒
today's behaviour (the request still succeeds).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from omnigent.identity.defaults import acting_identity_for
from omnigent.identity.signer import (
    HEADER_NAME,
    HmacAssertionSigner,
    encode_acting_identity,
)
from omnigent.identity.verifiers import HmacAssertionVerifier
from omnigent.runner import create_runner_app, tool_dispatch
from omnigent.server.principal import Principal
from tests.runner.helpers import NullServerClient

_SECRET = "test-acting-identity-secret"


@asynccontextmanager
async def _client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


def _build_app():
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    # Override the from_env() default with a configured verifier for the test.
    app.state.assertion_verifier = HmacAssertionVerifier(_SECRET)
    return app


def _post_tools_call(app, headers: dict[str, str]) -> tuple[int, dict]:
    """POST a bare runner-local tools/call; return (status, captured execute_tool kwargs)."""
    captured: dict = {}

    async def _fake_execute_tool(*, tool_name, arguments, acting_identity=None, **kw):
        captured["acting_identity"] = acting_identity
        return "ok"

    # mcp_execute does a call-time `from ... import execute_tool`, so patching the
    # module attribute is what it resolves.
    orig = tool_dispatch.execute_tool
    tool_dispatch.execute_tool = _fake_execute_tool

    async def _run():
        async with _client(app) as http:
            resp = await http.post(
                "/v1/sessions/conv1/mcp/execute",
                json={"method": "tools/call", "params": {"name": "sys_os_read", "arguments": {}}},
                headers=headers,
            )
            return resp.status_code, resp.text

    try:
        status, text = asyncio.run(_run())
    finally:
        tool_dispatch.execute_tool = orig
    assert status == 200, text
    return status, captured


def test_mcp_execute_header_carrier_reaches_execute_tool():
    app = _build_app()
    ident = acting_identity_for(
        Principal(user_id="alice@x", tenant_id="t1", roles=("admin",)), agent_id="maya"
    )
    token = encode_acting_identity(ident, HmacAssertionSigner(_SECRET))
    _, captured = _post_tools_call(app, {HEADER_NAME: token})
    ai = captured["acting_identity"]
    assert ai is not None
    assert ai.principal.user_id == "alice@x"
    assert ai.principal.tenant_id == "t1"
    assert ai.agent_id == "maya"


def test_mcp_execute_absent_header_is_none():
    # Spawn-safe / today's behaviour: no header ⇒ acting_identity None, request OK.
    _, captured = _post_tools_call(_build_app(), {})
    assert captured["acting_identity"] is None


@pytest.mark.parametrize("bad", ["garbage.token", "a.b.c"])
def test_mcp_execute_invalid_token_fails_open_to_none(bad):
    _, captured = _post_tools_call(_build_app(), {HEADER_NAME: bad})
    assert captured["acting_identity"] is None


def test_mcp_execute_wrong_secret_token_fails_open_to_none():
    ident = acting_identity_for(Principal(user_id="alice@x"), agent_id="maya")
    forged = encode_acting_identity(ident, HmacAssertionSigner("wrong-secret"))
    _, captured = _post_tools_call(_build_app(), {HEADER_NAME: forged})
    assert captured["acting_identity"] is None
