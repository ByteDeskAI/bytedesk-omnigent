"""First-party plugin for the ``omnigent.coordination`` subpackage (BDP-2509).

Dogfoods the kernel seam contract: instead of the coordination backplane being a
privileged, hard-wired default, this subpackage registers its **own existing**
concrete providers through the ``coordination_backplane`` :class:`PluggableRegistry`
seam — exactly the contract a third-party extension would use (Section 9.2, the
dogfooding argument). If the seam can host the first-party in-process + NATS
backplanes, it can host anyone's.

Per the Section 9.1 row, ``omnigent.coordination`` registers into a single kernel
seam — ``coordination_backplane`` — contributing:

  * ``inprocess`` — :class:`~omnigent.coordination.inprocess.InProcessBackplane`,
    the first-party **default** (single-replica, zero-dep).
  * ``nats`` — :class:`~omnigent.coordination.nats_backplane.NatsBackplane`, the
    optional first-party **alternate** (cross-replica via NATS JetStream).

The seam hook the kernel aggregates for this seam is
``coordination_backplane_providers`` (see ``omnigent/pluggable/manifest.py``
``SEAMS``), which returns a ``{name: factory}`` mapping — the same identity-port
shape as ``assertion_verifiers`` / ``outbound_credential_providers``. The SDK's
``@extension`` decorator has no member-decorator for this seam, so the hook is
written by hand on the class; the decorator only fills in hooks the author did
*not* define (``_set_if_absent``), leaving this one intact while still making the
instance conform to :class:`omnigent.kernel.extensions.OmnigentExtension`.

The factories themselves are **not** rewritten here — they are the subpackage's
already-existing ``_inprocess_factory`` / ``_nats_factory`` (in ``factory.py``),
imported lazily inside the hook to keep this module circular-import-safe and the
kernel domain-free (deferred-import pattern, NON-NEGOTIABLE rule 4).

This plugin is **not** wired into boot yet — the Integration phase does that
(Section 9.3 dependency order: it depends on the kernel only). It only needs to
import cleanly and expose the correct hook return shape.
"""

from __future__ import annotations

from collections.abc import Callable

from omnigent.sdk import extension


@extension(name="omnigent.coordination")
class CoordinationExtension:
    """First-party plugin: registers the coordination backplane providers.

    Contributes into the ``coordination_backplane`` kernel seam via the
    ``coordination_backplane_providers`` hook. ``requires`` is empty: per the
    Section 9.1 boot-order column this plugin depends on the kernel only.
    """

    def coordination_backplane_providers(self) -> dict[str, Callable[[], object]]:
        """Return ``{name: factory}`` for this subpackage's backplane providers.

        Reuses the subpackage's already-defined factories (dogfooding — the
        providers are not moved or rewritten here). Imported lazily so this
        module stays circular-import-safe and the kernel-light import path is
        preserved.

        * ``inprocess`` → :func:`~omnigent.coordination.factory._inprocess_factory`
          (the seam's first-party default).
        * ``nats`` → :func:`~omnigent.coordination.factory._nats_factory`
          (the optional first-party alternate; constructs a
          :class:`~omnigent.coordination.nats_backplane.NatsBackplane` and raises
          if ``OMNIGENT_NATS_URL`` is unset, deferred until the factory is
          actually resolved — never at registration time).
        """
        from omnigent.coordination.factory import (
            _inprocess_factory,
            _nats_factory,
        )

        return {
            "inprocess": _inprocess_factory,
            "nats": _nats_factory,
        }
