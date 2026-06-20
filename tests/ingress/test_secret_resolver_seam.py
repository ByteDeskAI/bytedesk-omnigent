"""Seam tests for the ingress webhook secret resolver Strategy (BDP-2349 #16).

Proves: the default resolver still reads the env, a custom resolver can be
injected via set_secret_resolver, and resetting to None restores the env default.
"""
from __future__ import annotations

import pytest

from bytedesk_omnigent import ingress


@pytest.fixture(autouse=True)
def _restore_resolver():
    yield
    ingress.set_secret_resolver(None)


def test_default_resolver_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_INGRESS_SECRET_TEAMCITY", "s3cr3t")
    assert ingress.resolve_secret("teamcity") == "s3cr3t"
    assert ingress.resolve_secret("teamcity") == ingress.default_secret_resolver(
        "teamcity"
    )


def test_unconfigured_source_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("OMNIGENT_INGRESS_SECRET_NOPE", raising=False)
    assert ingress.resolve_secret("nope") is None


def test_injected_resolver_wins() -> None:
    seen: list[str] = []

    def vault_resolver(source: str) -> str | None:
        seen.append(source)
        return f"vault-{source}"

    ingress.set_secret_resolver(vault_resolver)
    assert ingress.resolve_secret("github") == "vault-github"
    assert seen == ["github"]


def test_reset_restores_default(monkeypatch) -> None:
    ingress.set_secret_resolver(lambda source: "override")
    assert ingress.resolve_secret("x") == "override"
    ingress.set_secret_resolver(None)
    monkeypatch.delenv("OMNIGENT_INGRESS_SECRET_X", raising=False)
    assert ingress.resolve_secret("x") is None
