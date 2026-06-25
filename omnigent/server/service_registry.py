"""Strangler re-export shim — moved to ``omnigent.kernel.service_registry`` (BDP-2515).

The typed ``ServiceRegistry`` container relocated into the kernel package. This
shim keeps ``from omnigent.server.service_registry import ServiceRegistry`` working
until call sites migrate to the canonical kernel path (BDP-2516). Same class
object, no copy.
"""

from __future__ import annotations

from omnigent.kernel.service_registry import *  # noqa: F401,F403
from omnigent.kernel.service_registry import ServiceRegistry  # noqa: F401

__all__ = ["ServiceRegistry"]
