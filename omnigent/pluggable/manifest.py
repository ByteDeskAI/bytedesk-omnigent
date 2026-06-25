"""Strangler re-export shim — moved to ``omnigent.kernel.pluggable.manifest`` (BDP-2515).

Keeps ``from omnigent.pluggable.manifest import ...`` working until call sites
migrate to the canonical kernel path (BDP-2516). Same objects, no copies.
"""

from __future__ import annotations

from omnigent.kernel.pluggable.manifest import *  # noqa: F401,F403
from omnigent.kernel.pluggable.manifest import (  # noqa: F401  explicit public re-exports
    SEAMS,
    capability_manifest,
    discover_all_extensions,
)

__all__ = ["SEAMS", "capability_manifest", "discover_all_extensions"]
