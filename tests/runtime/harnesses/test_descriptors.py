"""Tests for the harness descriptor registry (BDP-2346).

``omnigent.runtime.harnesses.descriptors`` is the single source of truth for
harness identity: a :class:`~omnigent.pluggable.PluggableRegistry` of
:class:`HarnessDescriptor` values from which ``_HARNESS_MODULES``, the native
classification, and alias resolution are projected. These tests pin the registry's
shape and the projections.
"""

from __future__ import annotations

from omnigent.harness_aliases import HARNESS_ALIASES
from omnigent.pluggable import PluggableRegistry
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.descriptors import (
    HARNESS_DESCRIPTORS,
    HARNESS_REGISTRY,
    HarnessDescriptor,
    harness_modules,
    native_harness_ids,
    resolve,
)


def test_registry_is_a_pluggable_registry() -> None:
    """The harness seam is built on the generic PluggableRegistry."""
    assert isinstance(HARNESS_REGISTRY, PluggableRegistry)
    assert HARNESS_REGISTRY.seam == "harness"


def test_descriptors_map_matches_registry_names() -> None:
    """Every registered canonical id materializes to a descriptor."""
    assert set(HARNESS_DESCRIPTORS) == set(HARNESS_REGISTRY.names())
    for name, descriptor in HARNESS_DESCRIPTORS.items():
        assert isinstance(descriptor, HarnessDescriptor)
        assert descriptor.name == name


def test_harness_modules_projection_matches_package_dict() -> None:
    """harness_modules() reproduces the legacy _HARNESS_MODULES shape.

    Canonical-id and inline-alias keys both map to the descriptor's module path;
    descriptors with no module path are omitted. The package ``_HARNESS_MODULES``
    is exactly this projection at import time.
    """
    projected = harness_modules()
    # Every module-backed descriptor contributes its canonical id...
    for descriptor in HARNESS_DESCRIPTORS.values():
        if descriptor.module_path is None:
            assert descriptor.name not in projected
            continue
        assert projected[descriptor.name] == descriptor.module_path
        # ...and each of its aliases points at the same module.
        for alias in descriptor.aliases:
            assert projected[alias] == descriptor.module_path
    # The package dict is a fresh copy of this projection (mutable for tests).
    assert _HARNESS_MODULES == projected
    assert _HARNESS_MODULES is not projected


def test_harness_modules_returns_fresh_mutable_dict() -> None:
    """Each call returns an independent dict so callers can mutate safely."""
    a = harness_modules()
    b = harness_modules()
    assert a == b and a is not b
    a["test-only-harness"] = "tests.fake"
    assert "test-only-harness" not in harness_modules()


def test_native_harness_ids_are_descriptor_derived() -> None:
    """native_harness_ids() returns exactly the descriptors flagged native."""
    assert native_harness_ids() == frozenset(
        name for name, d in HARNESS_DESCRIPTORS.items() if d.is_native
    )
    assert "claude-native" in native_harness_ids()
    assert "claude-sdk" not in native_harness_ids()


def test_no_canonical_id_is_an_alias() -> None:
    """A user-facing alias never doubles as a canonical descriptor id."""
    for alias in HARNESS_ALIASES:
        assert alias not in HARNESS_DESCRIPTORS, alias


def test_resolve_canonical_and_alias_and_unknown() -> None:
    """resolve() accepts canonical ids and aliases; unknown / None → None."""
    assert resolve("codex").name == "codex"
    assert resolve("claude").name == "claude-sdk"
    assert resolve("nope") is None
    assert resolve(None) is None


def test_hermes_descriptor_present_in_default_set() -> None:
    """The ByteDesk hermes harness is a first-class descriptor (hard fork)."""
    hermes = HARNESS_DESCRIPTORS["hermes"]
    assert hermes.module_path == "bytedesk_omnigent.harnesses.hermes_native_harness"
    assert hermes.is_native is False


def test_extension_hook_is_a_safe_noop_when_absent() -> None:
    """Re-running discover_extensions with the harness hook never errors.

    No extension defines ``harness_descriptors`` today, so discovery is a no-op —
    the same safe seam the artifact-store reference registry relies on.
    """
    before = set(HARNESS_REGISTRY.names())
    HARNESS_REGISTRY.discover_extensions(hook="harness_descriptors")
    assert set(HARNESS_REGISTRY.names()) == before
