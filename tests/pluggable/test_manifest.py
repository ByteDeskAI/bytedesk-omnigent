"""Unit tests for the pluggable capability manifest (BDP-2374)."""

from __future__ import annotations

from omnigent.kernel.pluggable.manifest import (
    SEAMS,
    capability_manifest,
    discover_all_extensions,
)

# The seams expected to be live, with their per-environment override env var and
# the registered default impl. This is the regression net: if a seam is removed,
# renamed, or its default silently changes, this map drifts and a test fails.
_EXPECTED_SEAMS: dict[str, tuple[str, str | None]] = {
    # seam -> (override_env, default_impl)
    "harness": ("OMNIGENT_USE_HARNESS", "claude-sdk"),
    "artifact_store": ("OMNIGENT_USE_ARTIFACT_STORE", "local"),
    "agent_store": ("OMNIGENT_USE_AGENT_STORE", "nats"),
    # web_search has no registered default — selection is always explicit.
    "web_search": ("OMNIGENT_USE_WEB_SEARCH", None),
    "memory_embedder": ("OMNIGENT_USE_MEMORY_EMBEDDER", "fastembed"),
    "agent_memory": ("OMNIGENT_USE_AGENT_MEMORY", "composed"),
    "spec_source": ("OMNIGENT_USE_SPEC_SOURCE", "filesystem"),
    "coordination_backplane": ("OMNIGENT_USE_COORDINATION_BACKPLANE", "inprocess"),
    # Identity seams (adr-omnigent-pluggable-identity).
    "assertion_verifier": ("OMNIGENT_USE_ASSERTION_VERIFIER", "hmac"),
    "outbound_credential": ("OMNIGENT_USE_OUTBOUND_CREDENTIAL", "static_secret"),
    "authorizer": ("OMNIGENT_USE_AUTHORIZER", "owner_allow"),
}


def test_seams_table_lists_every_live_seam() -> None:
    """The SEAMS declaration covers exactly the expected seam set."""
    declared = {seam for seam, _accessor, _hook in SEAMS}
    assert declared == set(_EXPECTED_SEAMS)


def test_manifest_entry_shape_and_keys() -> None:
    """Each manifest entry carries the describe() keys + override_env."""
    manifest = capability_manifest()
    assert isinstance(manifest, list)
    assert {entry["seam"] for entry in manifest} == set(_EXPECTED_SEAMS)

    required = {"seam", "names", "active", "default", "override_env"}
    for entry in manifest:
        assert required <= set(entry), f"missing keys on {entry['seam']!r}: {entry}"
        assert "error" not in entry, f"seam {entry['seam']!r} failed to build: {entry}"
        assert isinstance(entry["names"], list)
        assert isinstance(entry["seam"], str)


def test_manifest_defaults_and_override_env_match_expected() -> None:
    """Each seam reports its expected default impl and override env var.

    Regression net: a silent default change (e.g. swapping the artifact-store
    default away from ``local`` or the embedder away from ``fastembed``) flips
    one of these and fails here instead of shipping unnoticed.
    """
    by_seam = {entry["seam"]: entry for entry in capability_manifest()}
    for seam, (override_env, default_impl) in _EXPECTED_SEAMS.items():
        entry = by_seam[seam]
        assert entry["override_env"] == override_env, seam
        assert entry["default"] == default_impl, seam
        # The default impl (when there is one) must be registered.
        if default_impl is not None:
            assert default_impl in entry["names"], seam


def test_manifest_is_json_serializable() -> None:
    """The manifest is plain JSON-serializable data."""
    import json

    dumped = json.dumps(capability_manifest())
    assert isinstance(dumped, str)


def test_seam_accessors_return_stable_registry_instances() -> None:
    """Every SEAMS accessor returns the same registry instance per process.

    BDP-2503's core cleanup depends on first-party and extension contributions
    landing on one stable seam plane. A SEAMS row that constructs a new registry
    on each call makes discovery/registration transient and prevents the core
    plugin path from becoming authoritative.
    """
    unstable = [seam for seam, accessor, _hook in SEAMS if accessor() is not accessor()]
    assert not unstable


def test_discover_all_extensions_is_safe_noop() -> None:
    """With no extensions installed, discovery runs without raising.

    It must be safe to call from server startup; today it is effectively a
    no-op (no extension defines any seam hook).
    """
    discover_all_extensions()
    # Idempotent: a second call must also not raise.
    discover_all_extensions()
