"""Tests for declarative provider-capability metadata (BDP-2340, Phase 6b)."""
from __future__ import annotations

from typing import ClassVar

from bytedesk_omnigent.provider_metadata import (
    ProviderKind,
    ProviderMetadata,
    ProviderMetadataMixin,
    metadata_for,
)


class _FakeLauncher:
    """Stand-in for an unmodified upstream SandboxLauncher: carries the same
    implicit capability ``ClassVar`` flags but does NOT mix in the helper."""

    provider: ClassVar[str] = "modal"
    wheel_build_index_url: ClassVar[str | None] = None
    supports_local_port_forward: ClassVar[bool] = False
    supports_cli_bootstrap: ClassVar[bool] = True


class _MixedLauncher(_FakeLauncher, ProviderMetadataMixin):
    """A launcher that opts into the mixin; default metadata must reflect the
    same flags it already declares (behavior unchanged)."""

    provider_kind: ClassVar[ProviderKind] = ProviderKind.SANDBOX


class _ForwardingLauncher(ProviderMetadataMixin):
    """A launcher with the optional local-port-forward capability set."""

    provider: ClassVar[str] = "lakebox"
    supports_local_port_forward: ClassVar[bool] = True
    supports_cli_bootstrap: ClassVar[bool] = True
    wheel_build_index_url: ClassVar[str | None] = "https://pypi.internal/simple"
    provider_kind: ClassVar[ProviderKind] = ProviderKind.SANDBOX
    provider_capabilities: ClassVar[dict[str, object]] = {"managed": True}


def test_metadata_is_frozen_value_object() -> None:
    meta = ProviderMetadata(name="modal", kind=ProviderKind.SANDBOX)
    # frozen=True — fields cannot be reassigned.
    try:
        meta.name = "daytona"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("ProviderMetadata should be frozen")


def test_mixin_default_reflects_implicit_flags_unchanged() -> None:
    meta = _MixedLauncher().metadata()
    assert meta.name == "modal"
    assert meta.kind == ProviderKind.SANDBOX
    # Mirrors the launcher's own ClassVar flags verbatim.
    assert meta.supports_local_port_forward is False
    assert meta.supports_cli_bootstrap is True
    assert meta.wheel_build_index_url is None
    assert meta.capabilities == {}


def test_mixin_carries_optional_and_extra_capabilities() -> None:
    meta = _ForwardingLauncher().metadata()
    assert meta.name == "lakebox"
    assert meta.supports_local_port_forward is True
    assert meta.wheel_build_index_url == "https://pypi.internal/simple"
    assert meta.capabilities == {"managed": True}
    # The mixin must copy the class dict so a mutation can't leak back.
    meta.capabilities["managed"] = False
    assert _ForwardingLauncher.provider_capabilities == {"managed": True}


def test_metadata_for_reconstructs_without_mixin() -> None:
    # An unmodified launcher (no mixin) still yields the same record via the
    # standalone helper, reading the very same implicit flags.
    meta = metadata_for(_FakeLauncher(), kind=ProviderKind.SANDBOX)
    assert meta.name == "modal"
    assert meta.kind == ProviderKind.SANDBOX
    assert meta.supports_local_port_forward is False
    assert meta.supports_cli_bootstrap is True
    assert meta.wheel_build_index_url is None


def test_metadata_for_prefers_own_accessor() -> None:
    # When the provider already exposes metadata(), the helper returns it as-is.
    meta = metadata_for(_MixedLauncher())
    assert meta.name == "modal"
    assert meta.kind == ProviderKind.SANDBOX


def test_harness_metadata_defaults_are_conservative() -> None:
    # A harness has no sandbox flags; the mixin's defaults stay False/None so a
    # harness opting in advertises only what it explicitly sets.
    class _FakeHarness(ProviderMetadataMixin):
        provider: ClassVar[str] = "grok-native"
        provider_kind: ClassVar[ProviderKind] = ProviderKind.HARNESS
        provider_capabilities: ClassVar[dict[str, object]] = {"native": True, "acp": True}

    meta = _FakeHarness().metadata()
    assert meta.name == "grok-native"
    assert meta.kind == ProviderKind.HARNESS
    assert meta.supports_local_port_forward is False
    assert meta.supports_cli_bootstrap is False
    assert meta.wheel_build_index_url is None
    assert meta.capabilities == {"native": True, "acp": True}


def test_to_dict_is_json_safe_round_trip() -> None:
    import json

    meta = ProviderMetadata(
        name="daytona",
        kind=ProviderKind.SANDBOX,
        supports_cli_bootstrap=False,
        capabilities={"managed_only": True},
    )
    payload = meta.to_dict()
    # Must serialize cleanly to a Text-as-JSON column (dual-DB rule, no JSONB).
    assert json.loads(json.dumps(payload)) == {
        "name": "daytona",
        "kind": "sandbox",
        "supports_local_port_forward": False,
        "supports_cli_bootstrap": False,
        "wheel_build_index_url": None,
        "capabilities": {"managed_only": True},
    }


def test_provider_kind_wire_values_match_old_string_constants() -> None:
    """BDP-2358: ProviderKind became a StrEnum; its members MUST stay
    byte-for-byte the old ``ClassVar[str]`` wire strings so persisted/serialized
    ``kind`` values remain compatible."""
    import json
    from enum import StrEnum

    assert issubclass(ProviderKind, StrEnum)
    # Exact wire strings (the closed set the rest of the codebase serializes).
    assert ProviderKind.SANDBOX.value == "sandbox"
    assert ProviderKind.HARNESS.value == "harness"
    assert ProviderKind.UNKNOWN.value == "unknown"
    # As a StrEnum, a member also compares equal to its bare wire string at runtime.
    assert ProviderKind.SANDBOX == "sandbox"  # type: ignore[comparison-overlap]
    # No accidental extra members crept in.
    assert {k.value for k in ProviderKind} == {"sandbox", "harness", "unknown"}
    # A StrEnum member JSON-serializes as its bare wire string (not "ProviderKind.SANDBOX").
    assert json.dumps(ProviderKind.HARNESS) == '"harness"'
    # Default kind is still UNKNOWN and bounded to the closed set.
    assert ProviderMetadata(name="x").kind.value == "unknown"
