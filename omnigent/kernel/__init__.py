"""``omnigent.kernel`` — TIER 1, the minimal boot-required microkernel (BDP-2514).

This package is the *physical* form of the kernel classification proved in
``docs/EXTENSION_FRAMEWORK_ANALYSIS.md`` §8 and the import-guard test. It is the
minimum set required to bring up an **empty** system and host plugins:

* the extension contract + discovery/install (:mod:`omnigent.kernel.extensions`),
* the pluggable-seam machinery (:mod:`omnigent.kernel.pluggable`),
* the lifecycle orchestrator (:mod:`omnigent.kernel.lifespan_phases`),
* the typed service registry (:mod:`omnigent.kernel.service_registry`).

**Invariant — domain-free + import-safe.** No kernel module imports a non-kernel
``omnigent.*`` module at module scope, and importing a kernel module must NOT drag
the FastAPI stack onto the runner subprocess hot path (FastAPI only under
``TYPE_CHECKING`` or inside function bodies). The two guard tests
(``tests/pluggable/test_kernel_import_guard.py`` and
``tests/runner/test_identity.py::test_importing_identity_does_not_pull_in_fastapi``)
are the executable form of this rule.

This ``__init__`` deliberately does **not** eagerly import the submodules — keeping
``import omnigent.kernel`` cheap and side-effect-free. The public surface is
re-exported lazily via :func:`__getattr__` so ``from omnigent.kernel import X``
works without paying for the whole kernel on a bare package import. ``app.py`` and
``container.py`` are facade-for-now (CORE) and are reached *from* the kernel rather
than relocated; they are not re-exported here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.kernel.extensions import (  # noqa: F401, I001
        OmnigentExtension,
        OmnigentExtensionLifecycle,
        assert_extension,
        discover_extensions,
        extension_background_factories,
        extension_connector_manifests,
        extension_connector_providers,
        extension_config_descriptors,
        extension_default_mcp_servers,
        extension_instruction_fragments,
        extension_policy_modules,
        extension_principal_resolvers,
        extension_secret_backends,
        extension_tool_factories,
        extension_tool_interceptors,
        get_extension,
        install_extensions,
    )
    from omnigent.kernel.lifespan_phases import (  # noqa: F401
        LifespanContext,
        LifespanCycleError,
        LifespanOrchestrator,
        LifespanPhase,
        build_default_lifespan_phases,
        topological_order,
    )
    from omnigent.kernel.pluggable import (  # noqa: F401
        PluggableRegistry,
        ProviderError,
        ProviderNotRegistered,
        ProviderUnavailable,
        ProviderUnconfigured,
        RegistryConflict,
    )
    from omnigent.kernel.service_registry import ServiceRegistry  # noqa: F401

#: Public kernel surface. ``__getattr__`` resolves each name to its owning kernel
#: submodule on first access, so the bare package import stays cheap.
_EXPORTS: dict[str, str] = {
    # extension contract + discovery/install + aggregators
    "OmnigentExtension": "omnigent.kernel.extensions",
    "OmnigentExtensionLifecycle": "omnigent.kernel.extensions",
    "discover_extensions": "omnigent.kernel.extensions",
    "install_extensions": "omnigent.kernel.extensions",
    "get_extension": "omnigent.kernel.extensions",
    "assert_extension": "omnigent.kernel.extensions",
    "extension_tool_factories": "omnigent.kernel.extensions",
    "extension_policy_modules": "omnigent.kernel.extensions",
    "extension_secret_backends": "omnigent.kernel.extensions",
    "extension_default_mcp_servers": "omnigent.kernel.extensions",
    "extension_instruction_fragments": "omnigent.kernel.extensions",
    "extension_principal_resolvers": "omnigent.kernel.extensions",
    "extension_background_factories": "omnigent.kernel.extensions",
    "extension_config_descriptors": "omnigent.kernel.extensions",
    "extension_connector_manifests": "omnigent.kernel.extensions",
    "extension_connector_providers": "omnigent.kernel.extensions",
    "extension_tool_interceptors": "omnigent.kernel.extensions",
    # pluggable-seam machinery + error taxonomy
    "PluggableRegistry": "omnigent.kernel.pluggable",
    "ProviderError": "omnigent.kernel.pluggable",
    "ProviderNotRegistered": "omnigent.kernel.pluggable",
    "ProviderUnconfigured": "omnigent.kernel.pluggable",
    "ProviderUnavailable": "omnigent.kernel.pluggable",
    "RegistryConflict": "omnigent.kernel.pluggable",
    # lifecycle orchestrator
    "LifespanPhase": "omnigent.kernel.lifespan_phases",
    "LifespanOrchestrator": "omnigent.kernel.lifespan_phases",
    "LifespanContext": "omnigent.kernel.lifespan_phases",
    "LifespanCycleError": "omnigent.kernel.lifespan_phases",
    "topological_order": "omnigent.kernel.lifespan_phases",
    "build_default_lifespan_phases": "omnigent.kernel.lifespan_phases",
    # typed service container
    "ServiceRegistry": "omnigent.kernel.service_registry",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> object:
    """Lazily resolve a public kernel symbol to its owning submodule (PEP 562)."""
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)
