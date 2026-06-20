"""Tests for omnigent.runtime.harnesses.provider (BDP-2327, BDP-2346).

The provider surface is now a thin view over the descriptor registry
(:mod:`omnigent.runtime.harnesses.descriptors`, the single source of truth,
BDP-2346). These tests pin the projection to the legacy identity sources
(``_HARNESS_MODULES`` / ``NATIVE_HARNESSES`` / the omnigent allowlist) so the
registry stays behaviorally identical to the four-source fold it replaced.
"""

from __future__ import annotations

import pytest

from omnigent.harness_aliases import HARNESS_ALIASES, NATIVE_HARNESSES
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.provider import (
    HARNESS_PROVIDERS,
    HarnessProvider,
    resolve,
)
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


def test_registry_covers_omnigent_allowlist() -> None:
    """Every canonical id in the omnigent allowlist has a descriptor."""
    missing = OMNIGENT_HARNESSES - set(HARNESS_PROVIDERS)
    assert not missing, f"allowlist harnesses without a provider: {sorted(missing)}"


def test_native_flag_agrees_with_legacy_source() -> None:
    """is_native mirrors membership in NATIVE_HARNESSES exactly."""
    for name, provider in HARNESS_PROVIDERS.items():
        assert provider.is_native == (name in NATIVE_HARNESSES), name


def test_module_path_agrees_with_harness_modules() -> None:
    """module_path mirrors the canonical entry in _HARNESS_MODULES."""
    for name, provider in HARNESS_PROVIDERS.items():
        assert provider.module_path == _HARNESS_MODULES.get(name), name


def test_no_canonical_name_is_a_known_alias() -> None:
    """Alias spellings (e.g. 'claude') never appear as canonical ids."""
    for alias in HARNESS_ALIASES:
        assert alias not in HARNESS_PROVIDERS, alias


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("claude", "claude-sdk"),
        ("grok", "grok-native"),
        ("openai-agents-sdk", "openai-agents"),
        ("google-antigravity", "antigravity"),
    ],
)
def test_resolve_accepts_aliases(alias: str, canonical: str) -> None:
    """resolve() maps a user-facing alias to its canonical descriptor."""
    provider = resolve(alias)
    assert isinstance(provider, HarnessProvider)
    assert provider.name == canonical
    assert alias in provider.aliases


def test_resolve_accepts_canonical_id() -> None:
    """resolve() returns the descriptor for a canonical id unchanged."""
    provider = resolve("codex-native")
    assert isinstance(provider, HarnessProvider)
    assert provider.name == "codex-native"
    assert provider.is_native is True


def test_resolve_unknown_and_none() -> None:
    """resolve() returns None for an unknown name and for None."""
    assert resolve("does-not-exist") is None
    assert resolve(None) is None


def test_claude_native_is_native_with_module() -> None:
    """A representative native harness resolves with native flag + module."""
    provider = HARNESS_PROVIDERS["claude-native"]
    assert provider.is_native is True
    assert provider.module_path == "omnigent.inner.claude_native_harness"


# ── BDP-2346: registry source-of-truth + full alias/native coverage ──


# Every known alias spelling → its canonical id. Mirrors HARNESS_ALIASES; pinned
# explicitly so a dropped alias fails here rather than silently disappearing.
_ALL_ALIAS_SPELLINGS = {
    "claude": "claude-sdk",
    "native-pi": "pi-native",
    "openai-agents-sdk": "openai-agents",
    "agy": "antigravity",
    "google-antigravity": "antigravity",
    "grok": "grok-native",
}


@pytest.mark.parametrize("alias,canonical", sorted(_ALL_ALIAS_SPELLINGS.items()))
def test_resolve_every_known_alias_spelling(alias: str, canonical: str) -> None:
    """Every alias in HARNESS_ALIASES resolves to its canonical descriptor."""
    provider = resolve(alias)
    assert isinstance(provider, HarnessProvider)
    assert provider.name == canonical
    assert alias in provider.aliases


def test_alias_set_matches_legacy_alias_source() -> None:
    """The registry's alias universe equals HARNESS_ALIASES exactly (no drift)."""
    registry_aliases = {
        alias for p in HARNESS_PROVIDERS.values() for alias in p.aliases
    }
    assert registry_aliases == set(HARNESS_ALIASES)


def test_native_vs_sdk_classification() -> None:
    """Native CLI harnesses flag is_native; SDK / HTTP harnesses do not."""
    for native in ("claude-native", "codex-native", "pi-native"):
        assert HARNESS_PROVIDERS[native].is_native is True, native
    for sdk in ("claude-sdk", "openai-agents", "antigravity", "databricks_supervisor"):
        assert HARNESS_PROVIDERS[sdk].is_native is False, sdk


def test_resolve_unknown_passes_through_as_none() -> None:
    """An unknown spelling is not coerced — resolve() returns None."""
    assert resolve("totally-made-up") is None
    assert resolve("") is None


def test_registry_exposes_full_descriptor_set() -> None:
    """HARNESS_PROVIDERS exposes one descriptor per registered canonical id."""
    from omnigent.runtime.harnesses.descriptors import (
        HARNESS_REGISTRY,
        HarnessDescriptor,
    )

    assert set(HARNESS_PROVIDERS) == set(HARNESS_REGISTRY.names())
    assert all(
        isinstance(p, HarnessDescriptor) for p in HARNESS_PROVIDERS.values()
    )
    # The descriptor set is a superset of every module-backed harness name.
    module_canonicals = {
        p.name for p in HARNESS_PROVIDERS.values() if p.module_path is not None
    }
    assert module_canonicals <= set(HARNESS_PROVIDERS)


def test_hermes_resolves_without_hardcoded_cross_package_string() -> None:
    """hermes resolves via the descriptor registry, not a literal in __init__.

    The cross-package ``'hermes' -> bytedesk_omnigent...`` string literal was
    deleted from ``_HARNESS_MODULES``; hermes is now a first-class descriptor in
    the default set, projected into the module map. Assert both the descriptor and
    the projection, and that the literal no longer appears in __init__.py source.
    """
    import inspect

    import omnigent.runtime.harnesses as harnesses_pkg

    provider = resolve("hermes")
    assert isinstance(provider, HarnessProvider)
    assert provider.name == "hermes"
    assert provider.module_path == "bytedesk_omnigent.harnesses.hermes_native_harness"
    assert (
        _HARNESS_MODULES["hermes"]
        == "bytedesk_omnigent.harnesses.hermes_native_harness"
    )
    # The hardcoded cross-package string must be gone from the package __init__.
    init_src = inspect.getsource(harnesses_pkg)
    assert "bytedesk_omnigent.harnesses.hermes_native_harness" not in init_src
