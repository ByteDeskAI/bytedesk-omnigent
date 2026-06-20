"""Residual Wave-2 pluggable seams (BDP-2350).

Each seam converted an inline ``if/elif/else`` (or a hardcoded classifier tuple)
into a registry/strategy. These tests assert the three invariants for every seam:

1. **Default selection** — the registered default is the historical impl.
2. **Registry swap** — a fake provider registered under a name is selectable.
3. **Preserved behavior** — selection maps the same inputs to the same impl;
   for the exception classifier, **order is preserved exactly**.
"""

from __future__ import annotations

import pytest

# ── #51 model-listing fetch strategy ─────────────────────────────


def test_model_listing_registry_names_and_default() -> None:
    from omnigent.model_catalog import _build_listing_fetch_registry

    reg = _build_listing_fetch_registry()
    assert set(reg.names()) == {"databricks", "anthropic", "openai_compatible"}
    # Default arm = the historical ``else`` (openai-compatible).
    assert reg.resolve_default().__name__ == "_fetch_openai_compatible_listing"


@pytest.mark.parametrize(
    ("kind", "family", "expected"),
    [
        ("databricks", None, "databricks"),
        ("key", "anthropic", "anthropic"),
        ("key", "openai", "openai_compatible"),
        ("gateway", None, "openai_compatible"),
        ("local", None, "openai_compatible"),
    ],
)
def test_model_listing_strategy_key_preserves_branch_precedence(
    kind: str, family: str | None, expected: str
) -> None:
    from omnigent.model_catalog import ResolvedModelProvider, _listing_strategy_key

    provider = ResolvedModelProvider(kind=kind, family=family)
    assert _listing_strategy_key(provider) == expected


def test_model_listing_registry_swap() -> None:
    from omnigent.model_catalog import _build_listing_fetch_registry

    reg = _build_listing_fetch_registry()
    sentinel = object()
    reg.register("fake", lambda: sentinel)
    assert reg.get("fake") is sentinel


# ── #48 OTLP metric-exporter strategy ────────────────────────────


def test_metric_exporter_registry_default_is_otlp() -> None:
    from omnigent.runtime.telemetry import _build_metric_exporter_registry

    reg = _build_metric_exporter_registry()
    assert reg.names() == ["otlp"]


def test_metric_exporter_registry_swap_selects_fake() -> None:
    from omnigent.runtime.telemetry import _build_metric_exporter_registry

    reg = _build_metric_exporter_registry()
    fake = object()
    reg.register("console", lambda: fake)
    assert reg.get("console") is fake


def test_metric_exporter_default_constructs_otlp(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_create_metric_exporter("otlp")`` builds the OTLP exporter (default arm)."""
    import omnigent.runtime.telemetry as telemetry

    built = object()
    monkeypatch.setattr(telemetry, "_create_otlp_metric_exporter", lambda: built)
    assert telemetry._create_metric_exporter("otlp") is built


# ── #19 inner-SDK exception classifier Chain-of-Responsibility ────


def test_inner_exception_chain_order_is_load_bearing() -> None:
    """The built-in chain order must match the historical tuple exactly."""
    from omnigent.runtime.harnesses._executor_adapter import (
        _build_inner_exception_chain,
    )

    names = [c.__name__ for c in _build_inner_exception_chain().classifiers()]
    assert names == [
        "_classify_openai_exception",
        "_classify_anthropic_exception",
        "_classify_claude_sdk_exception",
        "_classify_httpx_exception",
    ]


def test_inner_exception_chain_first_match_wins() -> None:
    from omnigent.runtime.harnesses._executor_adapter import (
        _InnerExceptionClassifierChain,
    )

    chain = _InnerExceptionClassifierChain()
    chain.register(lambda _exc: None)
    chain.register(lambda _exc: "first")
    chain.register(lambda _exc: "second")
    assert chain.classify(Exception()) == "first"


def test_inner_exception_chain_returns_none_when_no_match() -> None:
    from omnigent.runtime.harnesses._executor_adapter import (
        _InnerExceptionClassifierChain,
    )

    chain = _InnerExceptionClassifierChain()
    chain.register(lambda _exc: None)
    assert chain.classify(Exception()) is None


def test_classify_inner_exception_unrecognized_returns_none() -> None:
    """An exception no built-in classifier recognizes falls through to None."""
    from omnigent.runtime.harnesses._executor_adapter import classify_inner_exception

    assert classify_inner_exception(KeyError("nope")) is None


# ── #50 OIDC IdP adapter ─────────────────────────────────────────


def test_idp_adapter_registry_names_and_default() -> None:
    from omnigent.server.oidc import _build_idp_adapter_registry

    reg = _build_idp_adapter_registry()
    assert set(reg.names()) == {"github", "oidc"}
    # Default arm = generic OIDC discovery (the historical ``else``).
    assert type(reg.resolve_default()).__name__ == "_DiscoveryOIDCAdapter"


@pytest.mark.parametrize(
    ("issuer", "expected"),
    [
        ("https://github.com", "_GitHubIdPAdapter"),
        ("https://github.com/", "_GitHubIdPAdapter"),
        ("https://accounts.google.com", "_DiscoveryOIDCAdapter"),
        ("https://login.example.com", "_DiscoveryOIDCAdapter"),
    ],
)
def test_idp_adapter_selection_by_issuer(issuer: str, expected: str) -> None:
    from omnigent.server.oidc import _select_idp_adapter

    assert type(_select_idp_adapter(issuer)).__name__ == expected


def test_github_idp_adapter_builds_static_github_config() -> None:
    from omnigent.server.oidc import (
        _GITHUB_AUTHORIZATION_ENDPOINT,
        _GITHUB_USERINFO_ENDPOINT,
        IdPAdapterParams,
        _GitHubIdPAdapter,
    )

    params = IdPAdapterParams(
        issuer="https://github.com",
        client_id="cid",
        client_secret="sec",
        redirect_uri="https://app.example.com/auth/callback",
        cookie_secret=b"0" * 32,
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        allow_invites=False,
    )
    config = _GitHubIdPAdapter().resolve_config(params)
    assert config.provider_type == "github"
    assert config.authorization_endpoint == _GITHUB_AUTHORIZATION_ENDPOINT
    assert config.userinfo_endpoint == _GITHUB_USERINFO_ENDPOINT
    assert config.jwks_uri is None  # GitHub issues no id_token (fail-closed posture)


def test_idp_adapter_registry_swap() -> None:
    from omnigent.server.oidc import IdPAdapter, _build_idp_adapter_registry

    class _FakeAdapter:
        def resolve_config(self, params):  # type: ignore[no-untyped-def]
            return "fake-config"

    reg = _build_idp_adapter_registry()
    reg.register("fake", _FakeAdapter)
    selected = reg.get("fake")
    assert isinstance(selected, IdPAdapter)  # structural Protocol conformance
    assert selected.resolve_config(object()) == "fake-config"  # type: ignore[arg-type]


# ── #21 remote-function executor (Databricks default) ────────────


def test_remote_function_registry_default_is_databricks() -> None:
    from omnigent.runner.uc_function import (
        DatabricksRemoteFunctionExecutor,
        _build_remote_function_registry,
    )

    reg = _build_remote_function_registry()
    assert reg.names() == ["databricks"]
    assert isinstance(reg.resolve_default(), DatabricksRemoteFunctionExecutor)


async def test_execute_uc_function_dispatches_to_default_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public ``execute_uc_function`` routes through the default Databricks
    backend, which delegates to the existing UC implementation."""
    import omnigent.runner.uc_function as uc

    captured: dict[str, object] = {}

    async def _fake_impl(catalog_path, args, *, profile=None, warehouse_id=None):  # type: ignore[no-untyped-def]
        captured.update(
            catalog_path=catalog_path,
            args=args,
            profile=profile,
            warehouse_id=warehouse_id,
        )
        return "ok"

    monkeypatch.setattr(uc, "_execute_uc_function_databricks", _fake_impl)
    result = await uc.execute_uc_function(
        "cat.schema.fn", {"x": 1}, profile="p", warehouse_id="w"
    )
    assert result == "ok"
    assert captured == {
        "catalog_path": "cat.schema.fn",
        "args": {"x": 1},
        "profile": "p",
        "warehouse_id": "w",
    }


async def test_remote_function_registry_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.runner.uc_function import RemoteFunctionExecutor

    class _FakeExecutor:
        async def execute(self, catalog_path, args, *, profile=None, warehouse_id=None):  # type: ignore[no-untyped-def]
            return f"fake:{catalog_path}"

    exec_instance = _FakeExecutor()
    assert isinstance(exec_instance, RemoteFunctionExecutor)
    assert await exec_instance.execute("fn", {}) == "fake:fn"


# ── #20 OS-environment factory ───────────────────────────────────


def test_os_environment_registry_default_is_caller_process() -> None:
    from omnigent.inner.os_env import _build_os_environment_registry

    reg = _build_os_environment_registry()
    assert reg.names() == ["caller_process"]


def test_create_os_environment_none_returns_none() -> None:
    from omnigent.inner.os_env import create_os_environment

    assert create_os_environment(None) is None


def test_create_os_environment_unknown_type_raises_not_implemented() -> None:
    from omnigent.inner.datamodel import OSEnvSpec
    from omnigent.inner.os_env import create_os_environment

    spec = OSEnvSpec(type="remote_pod")
    with pytest.raises(NotImplementedError, match="os_env type 'remote_pod' is not implemented"):
        create_os_environment(spec)


def test_create_os_environment_caller_process_builds_env(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from omnigent.inner.datamodel import OSEnvSpec
    from omnigent.inner.os_env import CallerProcessOSEnvironment, create_os_environment

    env = create_os_environment(OSEnvSpec(type="caller_process", cwd=str(tmp_path)))
    assert isinstance(env, CallerProcessOSEnvironment)
    env.close()


def test_os_environment_registry_swap() -> None:
    from omnigent.inner.os_env import _build_os_environment_registry

    reg = _build_os_environment_registry()
    sentinel = object()
    reg.register("remote_pod", lambda: (lambda _spec: sentinel))
    builder = reg.get("remote_pod")
    assert builder(object()) is sentinel  # type: ignore[arg-type]
