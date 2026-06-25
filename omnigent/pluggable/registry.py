"""Strangler re-export shim — moved to ``omnigent.kernel.pluggable.registry`` (BDP-2515).

Keeps ``from omnigent.pluggable.registry import ...`` and
``import omnigent.pluggable.registry as regmod`` working until call sites migrate
to the canonical kernel path (BDP-2516).

**Patch caveat:** ``monkeypatch.setattr(omnigent.pluggable.registry,
"discover_extensions", ...)`` patches *this shim's* binding, not the canonical
``omnigent.kernel.pluggable.registry`` module that
:meth:`PluggableRegistry.discover_extensions` actually calls — so tests that patch
the discovery proxy MUST target the kernel module (done in lockstep for
``tests/pluggable/test_registry.py``). The explicit re-exports below exist only so
plain attribute *reads* through the old path keep resolving to the same objects.
"""

from __future__ import annotations

from omnigent.kernel.pluggable.registry import *  # noqa: F401,F403
from omnigent.kernel.pluggable.registry import (  # noqa: F401  explicit public re-exports
    PluggableRegistry,
    _override_env_name,
    discover_extensions,
)

__all__ = ["PluggableRegistry", "_override_env_name"]
