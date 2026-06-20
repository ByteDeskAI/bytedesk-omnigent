"""``omnigent.pluggable`` — the generic pluggable-seam framework (BDP-2345).

This is the keystone of the pluggability epic (BDP-2344): every seam that needs a
swappable backend uses the **same 4-invariant recipe** instead of bespoke if/else
selection and a per-seam exception. The recipe:

1. **A Protocol per seam.** Define the provider interface as a ``Protocol`` (ideally
   ``runtime_checkable``) so backends are structurally interchangeable and callers
   depend on the seam, not a concrete class.
2. **A registry + default fallback.** Hold named factories in a
   :class:`PluggableRegistry`; register a built-in default so the seam works
   out-of-the-box, and let extensions register alternatives.
3. **Entry-point discovery.** Aggregate extension-contributed providers through a
   per-seam hook on :class:`~omnigent.extensions.OmnigentExtension` via
   :meth:`PluggableRegistry.discover_extensions`, error-isolated so one bad
   extension can't break the others (mirrors the secret-backend chain).
4. **An optional strangler flag.** ``OMNIGENT_USE_<SEAM>`` selects the active impl
   by name at resolve time (default = the registered default), so a new backend can
   ship dark and be flipped on per-environment with no code change.

The worked reference seam is the artifact store
(:func:`omnigent.stores.factory._create_artifact_store`): its URI-scheme if/else is
now a ``PluggableRegistry`` keyed by scheme, default = local.

All seam failures raise from the shared taxonomy in :mod:`omnigent.pluggable.errors`
(:class:`ProviderError` + subclasses), so callers catch one base type.
"""

from __future__ import annotations

from omnigent.pluggable.errors import (
    ProviderError,
    ProviderNotRegistered,
    ProviderUnavailable,
    ProviderUnconfigured,
    RegistryConflict,
)
from omnigent.pluggable.registry import PluggableRegistry

__all__ = [
    "PluggableRegistry",
    "ProviderError",
    "ProviderNotRegistered",
    "ProviderUnconfigured",
    "ProviderUnavailable",
    "RegistryConflict",
]
