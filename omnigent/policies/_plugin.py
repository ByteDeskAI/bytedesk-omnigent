"""First-party plugin for the ``omnigent.policies`` subpackage (BDP-2509).

Dogfoods the kernel extension seam: instead of ``load_registry()`` reaching
straight into :data:`omnigent.policies.builtins.BUILTIN_POLICY_MODULES`, this
plugin contributes those same module paths through the ordinary
``OmnigentExtension.policy_modules()`` hook — the identical seam a third-party
extension uses (Section 9.2, "the dogfooding argument"). The seam aggregator
(``load_registry()`` / ``extension_policy_modules()``) stays unchanged; only the
*source* of the first-party modules moves from a hard-coded splice to a plugin
contribution (Section 9.1, the ``policies`` row).

This is an ordinary extension built with the public SDK ``@extension``
decorator — there is no privileged "core" wiring. Its instances satisfy the
kernel :class:`omnigent.kernel.extensions.OmnigentExtension` Protocol exactly as
``BytedeskExtension`` does.

NOT yet wired into boot: discovery + ``load_registry()`` integration is the
Integration phase's job. This module only needs to import cleanly (kernel-light,
no domain imports at module scope) and expose the correct hook returns. The
existing concrete policy modules are **not** moved or rewritten — they are
re-exposed in place, imported lazily inside the hook to stay circular-import
safe.
"""

from __future__ import annotations

from ..sdk import extension


@extension(name="omnigent.policies")
class PoliciesExtension:
    """Registers this subpackage's built-in policy modules into the ``policies``
    seam via the ``policy_modules`` hook.

    The contribution is the existing :data:`BUILTIN_POLICY_MODULES` list of
    dotted module paths — each already exposes a module-level ``POLICY_REGISTRY``
    that ``omnigent.policies.registry.load_registry`` scans unchanged. We hand
    back the real module paths (not SDK-synthesised policy modules) precisely
    because the providers already exist; this plugin only relocates *where the
    list comes from*, not *what is in it*.
    """

    def policy_modules(self) -> list[str]:
        # Deferred import: keeps this module kernel-light at import time and
        # avoids importing the concrete policy modules (and their transitive
        # domain deps) until the seam is actually aggregated. hasattr-probing
        # aggregators see this hook; absent it, nothing changes.
        from .builtins import BUILTIN_POLICY_MODULES

        return list(BUILTIN_POLICY_MODULES)


__all__ = ["PoliciesExtension"]
