"""Declarative provider-capability metadata (BDP-2340, Phase 6b, ADR-0143).

Today a provider's capabilities are scattered across implicit class-level hints:
a :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` carries
``provider`` / ``wheel_build_index_url`` / ``supports_local_port_forward`` /
``supports_cli_bootstrap`` ``ClassVar`` flags, and an inner harness is just a
name registered in ``omnigent.runtime.harnesses._HARNESS_MODULES`` with its
native / SDK / CLI nature described only in prose docstrings. Nothing exposes
that surface as a single queryable record.

This module adds that surface WITHOUT touching the upstream-tracked core: a
frozen :class:`ProviderMetadata` value object plus a :class:`ProviderMetadataMixin`
that any launcher or harness can opt into to expose its capabilities
declaratively. The mixin's default :meth:`~ProviderMetadataMixin.metadata`
reflects today's implicit ``ClassVar`` flags verbatim, so a provider that mixes
it in (or whose metadata is read via :func:`metadata_for`) behaves exactly as it
does now — this is purely additive read-side introspection.

Mirrors the package's store/ABC conventions: ``from __future__ import
annotations``, a plain frozen dataclass for the value object, and an accessor
that derives the default from existing attributes rather than duplicating them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

__all__ = [
    "ProviderKind",
    "ProviderMetadata",
    "ProviderMetadataMixin",
    "metadata_for",
]


# Coarse classification of what a provider IS. A ``StrEnum`` so each member IS
# its wire string (``ProviderKind.SANDBOX == "sandbox"`` and JSON-serializes as
# ``"sandbox"``) — wire-compatible with the stringly-typed ``provider`` /
# harness-name surface the rest of the codebase serializes, while catching typos
# and invalid values at author time.
class ProviderKind(StrEnum):
    """Coarse provider categories used by :attr:`ProviderMetadata.kind`."""

    SANDBOX = "sandbox"
    HARNESS = "harness"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderMetadata:
    """Immutable description of one provider's identity and capabilities.

    A single queryable record collapsing the implicit ``ClassVar`` capability
    flags (and prose-only nature hints) a launcher or harness carries today, so
    callers can introspect a provider declaratively instead of poking at
    individual class attributes.

    :param name: Short provider name, e.g. ``"modal"`` for a sandbox launcher
        or ``"grok-native"`` for a harness. Mirrors
        :attr:`~omnigent.onboarding.sandboxes.base.SandboxLauncher.provider`
        and the ``_HARNESS_MODULES`` registry key.
    :param kind: One of :class:`ProviderKind`'s constants.
    :param supports_local_port_forward: Whether the provider can bridge a local
        port into its sandbox (``ssh -L`` semantics). Sandbox-only; ``False``
        for harnesses and for sandbox providers without an inbound path.
    :param supports_cli_bootstrap: Whether the provider supports the CLI
        bootstrap flow (wheel shipping + streaming attach). Sandbox launchers
        default this to ``True``; managed-only launchers set it ``False``.
    :param wheel_build_index_url: Package index URL exported for the local wheel
        build, or ``None`` to use ambient configuration. Sandbox-only.
    :param capabilities: Free-form extra capability flags keyed by name, for
        provider-specific surface not promoted to a first-class field. Holds
        JSON-serializable scalars only (this maps to a Text-as-JSON column when
        persisted, never native JSONB — dual-DB rule).
    """

    name: str
    kind: ProviderKind = ProviderKind.UNKNOWN
    supports_local_port_forward: bool = False
    supports_cli_bootstrap: bool = False
    wheel_build_index_url: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-safe dict.

        Stable wire shape for persistence (a Text column holding this JSON, per
        the dual-DB rule) or for an introspection API response.

        :returns: A dict with the same keys as the dataclass fields.
        """
        return {
            "name": self.name,
            "kind": self.kind,
            "supports_local_port_forward": self.supports_local_port_forward,
            "supports_cli_bootstrap": self.supports_cli_bootstrap,
            "wheel_build_index_url": self.wheel_build_index_url,
            "capabilities": dict(self.capabilities),
        }


class ProviderMetadataMixin:
    """Opt-in mixin letting a launcher or harness expose :class:`ProviderMetadata`.

    Mix into a provider class to gain a :meth:`metadata` accessor. The default
    implementation derives the record from the implicit capability ``ClassVar``
    flags the provider already declares (``provider`` /
    ``supports_local_port_forward`` / ``supports_cli_bootstrap`` /
    ``wheel_build_index_url``), so behavior is unchanged: an existing sandbox
    launcher that mixes this in reports exactly the flags it carries today.

    A subclass that needs to advertise extra capabilities sets the class-level
    :attr:`provider_capabilities` dict, or overrides :meth:`metadata` outright.
    """

    # Coarse classification used when building the default metadata. Subclasses
    # may override; sandbox launchers leave the default since this mixin's
    # default reads launcher ``ClassVar`` flags.
    provider_kind: ClassVar[ProviderKind] = ProviderKind.UNKNOWN

    # Extra, provider-specific capability flags merged into
    # :attr:`ProviderMetadata.capabilities`. JSON-serializable scalars only.
    provider_capabilities: ClassVar[dict[str, Any]] = {}

    def metadata(self) -> ProviderMetadata:
        """Build this provider's declarative capability record.

        The default reflects today's implicit flags verbatim — reading the
        provider's own ``ClassVar`` attributes when present and falling back to
        the conservative defaults otherwise — so mixing this in never changes
        behavior.

        :returns: The provider's :class:`ProviderMetadata`.
        """
        name = getattr(self, "provider", None) or type(self).__name__
        return ProviderMetadata(
            name=str(name),
            kind=self.provider_kind,
            supports_local_port_forward=bool(
                getattr(self, "supports_local_port_forward", False)
            ),
            supports_cli_bootstrap=bool(getattr(self, "supports_cli_bootstrap", False)),
            wheel_build_index_url=getattr(self, "wheel_build_index_url", None),
            capabilities=dict(self.provider_capabilities),
        )


def metadata_for(
    provider: object, *, kind: ProviderKind = ProviderKind.UNKNOWN
) -> ProviderMetadata:
    """Derive :class:`ProviderMetadata` for any provider object.

    Helper for callers that want a metadata record from a provider that does NOT
    mix in :class:`ProviderMetadataMixin` (e.g. an unmodified upstream
    :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`). If the object
    already exposes a ``metadata()`` accessor it is used as-is; otherwise the
    record is reconstructed from the same implicit ``ClassVar`` flags the mixin
    reads, so the result matches today's behavior without editing the provider.

    :param provider: A launcher / harness instance (or class) to introspect.
    :param kind: Coarse classification to stamp when reconstructing — pass
        :attr:`ProviderKind.SANDBOX` for a launcher. Ignored when *provider*
        supplies its own ``metadata()``.
    :returns: The provider's :class:`ProviderMetadata`.
    """
    own = getattr(provider, "metadata", None)
    if callable(own):
        result = own()
        if isinstance(result, ProviderMetadata):
            return result
    name = getattr(provider, "provider", None) or type(provider).__name__
    return ProviderMetadata(
        name=str(name),
        kind=kind,
        supports_local_port_forward=bool(
            getattr(provider, "supports_local_port_forward", False)
        ),
        supports_cli_bootstrap=bool(getattr(provider, "supports_cli_bootstrap", False)),
        wheel_build_index_url=getattr(provider, "wheel_build_index_url", None),
        capabilities={},
    )
