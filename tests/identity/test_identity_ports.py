"""Unit tests for the pluggable identity ports, defaults, and secure invariants.

Standalone (no server, no extensions): proves every seam resolves a working
default, the HMAC verifier's secure-default invariants (require ``exp``), the
mint strategies reproduce today's egress, and any subpart is swappable via the
``OMNIGENT_USE_<SEAM>`` strangler env and the extension hook — the "acts as a
product, every piece replaceable" contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import sys
import time

import pytest

from omnigent.identity import ActingIdentity, Credential, Decision
from omnigent.identity.defaults import (
    OwnerAllowAuthorizer,
    StaticSecretProvider,
    acting_identity_for,
)
from omnigent.identity.mint import (
    MINT_REGISTRY,
    ClientCredentialsMintStrategy,
    PassThroughMintStrategy,
    StaticMintStrategy,
)
from omnigent.identity.ports import (
    AssertionVerifier,
    AuthorizationProvider,
    MintStrategy,
    OutboundCredentialProvider,
)
from omnigent.identity.registry import (
    build_assertion_verifier_registry,
    build_authorizer_registry,
    build_outbound_credential_registry,
)
from omnigent.identity.verifiers import HmacAssertionVerifier
from omnigent.kernel.pluggable.errors import ProviderNotRegistered
from omnigent.server.principal import Principal

_SECRET = "test-shared-secret"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _make_token(payload: dict, secret: str = _SECRET) -> str:
    payload_bytes = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64(payload_bytes)}.{_b64(sig)}"


# ── value objects ────────────────────────────────────────────────────────────


def test_credential_header_property():
    cred = Credential(header_value="Bearer xyz")
    assert cred.header == {"Authorization": "Bearer xyz"}
    assert Credential("k", header_name="X-Api-Key").header == {"X-Api-Key": "k"}


def test_acting_identity_for_passes_through():
    p = Principal(user_id="alice", tenant_id="t1")
    ident = acting_identity_for(p, agent_id="ag_1", delegation=["root"])
    assert ident == ActingIdentity(principal=p, agent_id="ag_1", delegation=("root",))
    # Standalone default: no principal, no agent.
    assert acting_identity_for() == ActingIdentity(principal=None, agent_id=None, delegation=())


# ── HmacAssertionVerifier: secure-default invariants ─────────────────────────


def test_hmac_verifier_accepts_valid_token():
    v = HmacAssertionVerifier(_SECRET)
    payload = {"user_id": "alice", "exp": time.time() + 300}
    assert v.verify(_make_token(payload)) == payload


def test_hmac_verifier_rejects_missing_exp():
    # SECURE DEFAULT: a token with no exp must NOT be accepted (was never-expires).
    v = HmacAssertionVerifier(_SECRET)
    assert v.verify(_make_token({"user_id": "alice"})) is None


def test_hmac_verifier_rejects_non_numeric_exp():
    v = HmacAssertionVerifier(_SECRET)
    assert v.verify(_make_token({"user_id": "alice", "exp": "soon"})) is None


def test_hmac_verifier_rejects_bool_exp():
    # isinstance(True, int) is True in Python — exp=True must still be rejected.
    v = HmacAssertionVerifier(_SECRET)
    assert v.verify(_make_token({"user_id": "alice", "exp": True})) is None


def test_hmac_verifier_rejects_expired():
    v = HmacAssertionVerifier(_SECRET)
    assert v.verify(_make_token({"user_id": "alice", "exp": time.time() - 1000})) is None


def test_hmac_verifier_rejects_signature_mismatch():
    v = HmacAssertionVerifier(_SECRET)
    forged = _make_token({"user_id": "alice", "exp": time.time() + 300}, secret="wrong")
    assert v.verify(forged) is None


def test_hmac_verifier_rejects_malformed():
    v = HmacAssertionVerifier(_SECRET)
    assert v.verify("not-a-token") is None
    assert v.verify("only.") is None
    assert v.verify(".onlysig") is None


def test_hmac_verifier_unconfigured_fails_closed(monkeypatch):
    monkeypatch.delenv("OMNIGENT_ASSERTION_HMAC_SECRET", raising=False)
    v = HmacAssertionVerifier.from_env()
    # No secret → trust nothing, even a structurally valid (unsigned-for-us) token.
    assert v.verify(_make_token({"user_id": "alice", "exp": time.time() + 300})) is None


def test_hmac_verifier_require_exp_opt_out():
    v = HmacAssertionVerifier(_SECRET, require_exp=False)
    payload = {"user_id": "alice"}
    assert v.verify(_make_token(payload)) == payload


# ── MintStrategy: reproduces today's egress ──────────────────────────────────


def test_static_strategy_resolves_secret(monkeypatch):
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda name: "sk-123")
    cred = StaticMintStrategy().mint(
        identity=None, integration="jira", config={"secret_ref": "JIRA_API_TOKEN"}
    )
    assert cred.header_value == "Bearer sk-123"


def test_static_strategy_custom_scheme_and_header(monkeypatch):
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda name: "raw-key")
    cred = StaticMintStrategy().mint(
        identity=None,
        integration="svc",
        config={"secret_ref": "K", "scheme": "", "header_name": "X-Api-Key"},
    )
    assert cred.header == {"X-Api-Key": "raw-key"}


def test_static_strategy_requires_secret_ref():
    with pytest.raises(ValueError):
        StaticMintStrategy().mint(identity=None, integration="x", config={})


def test_static_strategy_missing_secret_raises(monkeypatch):
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda name: None)
    with pytest.raises(RuntimeError):
        StaticMintStrategy().mint(identity=None, integration="x", config={"secret_ref": "absent"})


def test_client_credentials_strategy_delegates(monkeypatch):
    monkeypatch.setattr("omnigent.tools.mcp._resolve_oauth_token", lambda oauth: "oauth-tok")
    cred = ClientCredentialsMintStrategy().mint(
        identity=None, integration="mcp", config={"oauth": object()}
    )
    assert cred.header_value == "Bearer oauth-tok"


def test_pass_through_strategy_delegates(monkeypatch):
    monkeypatch.setattr("omnigent.tools.mcp._resolve_databricks_token", lambda profile: "dbx-tok")
    cred = PassThroughMintStrategy().mint(
        identity=None, integration="dbx", config={"profile": "DEFAULT"}
    )
    assert cred.header_value == "Bearer dbx-tok"


def test_mint_registry_default_is_static():
    assert MINT_REGISTRY.resolve_default().name == "static"
    assert MINT_REGISTRY.get("client_credentials").name == "client_credentials"
    assert MINT_REGISTRY.get("pass_through").name == "pass_through"


# ── default providers + degrade-to-default ───────────────────────────────────


def test_static_secret_provider_none_config_returns_none():
    # Degrade-to-default: no config to resolve a secret → no credential, no raise.
    assert StaticSecretProvider().mint(identity=None, integration="x", config=None) is None


def test_static_secret_provider_delegates_to_strategy(monkeypatch):
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda name: "sk")
    cred = StaticSecretProvider().mint(
        identity=None, integration="jira", config={"secret_ref": "JIRA_API_TOKEN"}
    )
    assert cred is not None and cred.header_value == "Bearer sk"


def test_owner_allow_authorizer_allows():
    d = OwnerAllowAuthorizer().decide(identity=None, action="read", resource="x")
    assert isinstance(d, Decision) and d.allowed is True


# ── pluggability: defaults resolve + any subpart is swappable ─────────────────


def test_registries_resolve_their_defaults():
    assert build_assertion_verifier_registry().resolve_default().name == "hmac"
    assert build_outbound_credential_registry().resolve_default().name == "static_secret"
    assert build_authorizer_registry().resolve_default().name == "owner_allow"


def test_strangler_env_swaps_the_active_impl(monkeypatch):
    # User's explicit ask: any piece can be replaced. Register an alternative and
    # prove OMNIGENT_USE_<SEAM> selects it over the default.
    class FakeAuthorizer:
        name = "deny_all"

        def decide(self, *, identity, action, resource):
            return Decision(allowed=False, reason="fake deny")

    reg = build_authorizer_registry()
    reg.register("deny_all", FakeAuthorizer)
    monkeypatch.setenv("OMNIGENT_USE_AUTHORIZER", "deny_all")
    assert reg.resolve_default().name == "deny_all"


def test_extension_hook_contributes_a_verifier(monkeypatch):
    # Prove the extension discovery path wires a consumer-supplied subpart.
    class FakeExt:
        name = "fake"

        def assertion_verifiers(self):
            return {"fake_verifier": lambda: HmacAssertionVerifier("ext-secret")}

    monkeypatch.setattr("omnigent.kernel.pluggable.registry.discover_extensions", lambda: [FakeExt()])
    reg = build_assertion_verifier_registry()
    reg.discover_extensions(hook="assertion_verifiers")
    assert "fake_verifier" in reg.names()
    assert reg.get("fake_verifier").name == "hmac"


# ── manifest projection ──────────────────────────────────────────────────────


def test_capability_manifest_lists_identity_seams():
    from omnigent.kernel.pluggable.manifest import capability_manifest

    seams = {entry["seam"]: entry for entry in capability_manifest()}
    for seam, default in (
        ("assertion_verifier", "hmac"),
        ("outbound_credential", "static_secret"),
        ("authorizer", "owner_allow"),
    ):
        assert seam in seams, f"{seam} missing from capability manifest"
        assert seams[seam]["default"] == default
        assert seams[seam]["override_env"] == f"OMNIGENT_USE_{seam.upper()}"


# ── propagation seam: ToolContext carries ActingIdentity (additive None) ──────


def test_tool_context_acting_identity_seam():
    from omnigent.tools.base import ToolContext

    # Additive default: existing construction is unchanged (agent→subagent safe).
    ctx = ToolContext(task_id="t", agent_id="ag")
    assert ctx.acting_identity is None

    # Carries an identity when supplied — the contract tools target.
    ident = acting_identity_for(principal=Principal(user_id="alice"), agent_id="ag")
    ctx2 = ToolContext(task_id="t", agent_id="ag", acting_identity=ident)
    assert ctx2.acting_identity is ident
    assert ctx2.acting_identity.principal.user_id == "alice"


# ── every subpart replaceable: swap proven for ALL THREE seams ────────────────


def test_strangler_env_swaps_assertion_and_outbound(monkeypatch):
    # The authorizer swap is covered above; complete the contract for the other
    # two seams so "any piece replaceable" is proven for all three.
    class FakeVerifier:
        name = "fake_verifier"

        def verify(self, header):
            return None

    av = build_assertion_verifier_registry()
    av.register("fake_verifier", FakeVerifier)
    monkeypatch.setenv("OMNIGENT_USE_ASSERTION_VERIFIER", "fake_verifier")
    assert av.resolve_default().name == "fake_verifier"

    class FakeProvider:
        name = "fake_outbound"

        def mint(self, *, identity, integration, config=None):
            return None

    oc = build_outbound_credential_registry()
    oc.register("fake_outbound", FakeProvider)
    monkeypatch.setenv("OMNIGENT_USE_OUTBOUND_CREDENTIAL", "fake_outbound")
    assert oc.resolve_default().name == "fake_outbound"


def test_extension_hook_contributes_outbound_and_authorizer(monkeypatch):
    # Mirror the verifier-hook test for the other two seams: the per-seam hook
    # name wiring in the SEAMS table (historically fragile) is correct for each.
    class FakeProvider:
        name = "fake_provider"

        def mint(self, *, identity, integration, config=None):
            return None

    class FakeAuthorizer:
        name = "fake_authorizer"

        def decide(self, *, identity, action, resource):
            return Decision(allowed=False, reason="fake")

    class FakeExt:
        name = "fake"

        def outbound_credential_providers(self):
            return {"fake_provider": FakeProvider}

        def authorization_providers(self):
            return {"fake_authorizer": FakeAuthorizer}

    monkeypatch.setattr("omnigent.kernel.pluggable.registry.discover_extensions", lambda: [FakeExt()])

    out = build_outbound_credential_registry()
    out.discover_extensions(hook="outbound_credential_providers")
    assert "fake_provider" in out.names()

    authz = build_authorizer_registry()
    authz.discover_extensions(hook="authorization_providers")
    assert "fake_authorizer" in authz.names()


# ── duck-typed Protocol conformance + error path ──────────────────────────────


def test_ports_are_runtime_checkable_conformant():
    # The whole pluggability premise is structural (runtime_checkable) duck typing.
    assert isinstance(HmacAssertionVerifier(None), AssertionVerifier)
    assert isinstance(StaticSecretProvider(), OutboundCredentialProvider)
    assert isinstance(OwnerAllowAuthorizer(), AuthorizationProvider)
    assert isinstance(StaticMintStrategy(), MintStrategy)


def test_static_secret_provider_unknown_strategy_raises():
    with pytest.raises(ProviderNotRegistered):
        StaticSecretProvider().mint(
            identity=None, integration="x", config={"strategy": "nope", "secret_ref": "r"}
        )


# ── load-bearing design claim: safe on the runner hot path ────────────────────


def test_identity_package_import_is_runner_light():
    # The identity package must NOT drag the FastAPI/server-app graph onto the
    # runner hot path. Import it in a fresh subprocess (cwd = the worktree, so it
    # resolves to the branch under test) and assert no heavy module loaded.
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    code = (
        "import omnigent.identity.mint, omnigent.identity.registry, "
        "omnigent.identity.verifiers, omnigent.identity.defaults, sys; "
        "bad=[m for m in ('fastapi','starlette','omnigent.server.app') if m in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"identity import pulled heavy modules onto the runner path: "
        f"{proc.stdout.strip()!r} / {proc.stderr.strip()!r}"
    )
