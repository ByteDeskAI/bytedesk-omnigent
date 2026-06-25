"""Strangler re-export shim — ``omnigent.pluggable`` moved to ``omnigent.kernel.pluggable`` (BDP-2515).

The pluggable-seam framework physically relocated into the kernel package. This
shim keeps every existing ``from omnigent.pluggable import ...`` import working
unchanged; call sites migrate to the canonical ``omnigent.kernel.pluggable`` path
in a later stage (BDP-2516), after which this shim is deleted.

Both import paths resolve to the *same* class objects (verified by identity
assertion in the kernel guard test) — there is no parallel machinery.
"""

from __future__ import annotations

from omnigent.kernel.pluggable import *  # noqa: F401,F403
from omnigent.kernel.pluggable import (  # noqa: F401  explicit public re-exports
    PluggableRegistry,
    ProviderError,
    ProviderNotRegistered,
    ProviderUnavailable,
    ProviderUnconfigured,
    RegistryConflict,
)

__all__ = [
    "PluggableRegistry",
    "ProviderError",
    "ProviderNotRegistered",
    "ProviderUnconfigured",
    "ProviderUnavailable",
    "RegistryConflict",
]
