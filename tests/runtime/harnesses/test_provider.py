"""Tests for omnigent.runtime.harnesses.provider (BDP-2327, Phase 1).

The provider registry is built at import from the four legacy
harness-identity sources; these tests pin it to those sources so a
future edit to either side fails loudly instead of drifting.
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
