"""KERNEL — the minimum code required to boot and host extensions.

Nothing in this package is domain-specific. It is the part that never changes
when you add a capability:

  * :mod:`.protocol`   — the ``Extension`` contract + lifecycle stages.
  * :mod:`.registry`   — ``PluggableRegistry[T]``, the per-seam factory store.
  * :mod:`.host`       — ``Host``: lifecycle engine, seams, service container.
  * :mod:`.discovery`  — entry-point + env-var self-registration.

Tiering:  KERNEL (here)  →  CORE (kernel + first-party extensions)  →  EXTENSIONS.
"""

from __future__ import annotations

from .di import Container, DIResolutionError, Lifetime
from .discovery import discover_extensions, register_entry_point
from .host import KERNEL_SEAMS, Host
from .protocol import LIFECYCLE_STAGES, Extension
from .registry import PluggableRegistry, ProviderNotRegistered, RegistryConflict

__all__ = [
    "Container",
    "DIResolutionError",
    "Lifetime",
    "Extension",
    "LIFECYCLE_STAGES",
    "Host",
    "KERNEL_SEAMS",
    "PluggableRegistry",
    "ProviderNotRegistered",
    "RegistryConflict",
    "discover_extensions",
    "register_entry_point",
]
