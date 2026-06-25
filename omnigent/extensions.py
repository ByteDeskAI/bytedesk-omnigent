"""Strangler re-export shim — moved to ``omnigent.kernel.extensions`` (BDP-2515).

The generic first-party extension seam (the ``OmnigentExtension`` Protocol,
``discover_extensions``/``install_extensions``, and the ``extension_*()``
aggregators) physically relocated into the kernel package. This shim keeps every
existing ``from omnigent.extensions import ...`` import working unchanged; call
sites migrate to the canonical ``omnigent.kernel.extensions`` path in a later
stage (BDP-2516), after which this shim is deleted. Same objects, no copies.
"""

from __future__ import annotations

from omnigent.kernel.extensions import *  # noqa: F401,F403
from omnigent.kernel.extensions import (  # noqa: F401  explicit public re-exports
    DISABLED_ENV_VAR,
    ENTRY_POINT_GROUP,
    ENV_VAR,
    OmnigentExtension,
    OmnigentExtensionLifecycle,
    assert_extension,
    discover_extensions,
    extension_background_factories,
    extension_config_descriptors,
    extension_default_mcp_servers,
    extension_policy_modules,
    extension_principal_resolvers,
    extension_secret_backends,
    extension_tool_factories,
    extension_tool_interceptors,
    get_extension,
    install_extensions,
)

__all__ = [
    "DISABLED_ENV_VAR",
    "ENTRY_POINT_GROUP",
    "ENV_VAR",
    "OmnigentExtension",
    "OmnigentExtensionLifecycle",
    "assert_extension",
    "discover_extensions",
    "extension_background_factories",
    "extension_config_descriptors",
    "extension_default_mcp_servers",
    "extension_policy_modules",
    "extension_principal_resolvers",
    "extension_secret_backends",
    "extension_tool_factories",
    "extension_tool_interceptors",
    "get_extension",
    "install_extensions",
]
