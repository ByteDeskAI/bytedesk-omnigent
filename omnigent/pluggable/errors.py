"""Strangler re-export shim — moved to ``omnigent.kernel.pluggable.errors`` (BDP-2515).

Keeps ``from omnigent.pluggable.errors import ...`` working until call sites are
migrated to the canonical kernel path (BDP-2516). Same class objects, no copies.
"""

from __future__ import annotations

from omnigent.kernel.pluggable.errors import *  # noqa: F401,F403
from omnigent.kernel.pluggable.errors import (  # noqa: F401  explicit public re-exports
    ProviderError,
    ProviderNotRegistered,
    ProviderUnavailable,
    ProviderUnconfigured,
    RegistryConflict,
)

__all__ = [
    "ProviderError",
    "ProviderNotRegistered",
    "ProviderUnconfigured",
    "ProviderUnavailable",
    "RegistryConflict",
]
