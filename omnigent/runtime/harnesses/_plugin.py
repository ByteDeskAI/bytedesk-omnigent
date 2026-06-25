"""First-party ``omnigent.harnesses`` plugin — the harness seam, dogfooded (BDP-2509).

This module is the SDK-shaped expression of the first-party harness set: a single
class, decorated with :func:`omnigent.sdk.extension`, that registers this
subpackage's *already-existing* default harness descriptors into the kernel
``harness`` :class:`~omnigent.kernel.pluggable.PluggableRegistry` seam through its
``harness_descriptors`` hook — the same hook a third-party extension implements
(Section 9.1 ``harnesses`` row; Section 9.2 dogfooding argument).

Why a hand-written ``harness_descriptors`` rather than per-harness ``@harness``
methods: the descriptor set already lives in exactly one place —
:func:`omnigent.runtime.harnesses.descriptors.harness_descriptors`, which projects
:data:`~omnigent.runtime.harnesses.descriptors._FIRST_PARTY_DESCRIPTORS` into the
``{canonical_id: () -> HarnessDescriptor}`` hook shape from the existing concrete
default classes (claude-sdk, codex, pi, … plus the cross-package ``hermes``). This
plugin **reuses** that single source of truth instead of restating each name /
module_path / aliases / is_native triple via a ``@harness`` decorator (which would
duplicate harness identity and reintroduce the coupling BDP-2346 removed). The
``@extension`` class decorator only synthesises a ``harness_descriptors`` hook when
the author has not written one (``_set_if_absent``), so the hand-written method
below is authoritative and feeds the kernel's
:meth:`PluggableRegistry.discover_extensions` exactly like a synthesised one.

The heavy / domain import (``descriptors``) is deferred inside the hook body so
importing this module stays kernel-light and circular-import-safe — importing
``omnigent.runtime.harnesses.descriptors`` at module scope would pull the harness
registry construction onto this module's import (BDP-2371 hot-path discipline).

This plugin is **not** wired into boot here. It only needs to import cleanly and
expose the correct hook return; the Integration phase adds it to the first-party
plugin set the composition root installs.
"""

from __future__ import annotations

from collections.abc import Callable

from omnigent.sdk import extension


@extension(name="omnigent.harnesses", requires=("omnigent.stores",))
class HarnessesPlugin:
    """The built-in harnesses, contributed through the ``harness`` seam.

    ``requires=("omnigent.stores",)`` mirrors the boot-order dependency in the
    Section 9.1 table (harness runners read/write artifacts via the stores
    plugin's providers); the kernel surfaces a missing/out-of-order dependency
    as a clear boot error rather than a mysterious ``None``.
    """

    def harness_descriptors(self) -> dict[str, Callable[[], object]]:
        """The first-party harness descriptors, in the ``harness_descriptors`` hook shape.

        Delegates to the subpackage's existing single source of truth so the
        descriptor set is declared exactly once. The return is the same
        ``{canonical_id: () -> HarnessDescriptor}`` mapping the ``harness`` seam's
        :meth:`PluggableRegistry.discover_extensions` consumes — one zero-arg
        factory per canonical harness id, fresh per call.

        The import is deferred to the call site to keep module import kernel-light
        and circular-import-safe.

        :returns: ``{canonical_id: () -> HarnessDescriptor}`` for every first-party
            harness.
        """
        from omnigent.runtime.harnesses.descriptors import harness_descriptors

        return harness_descriptors()


__all__ = ["HarnessesPlugin"]
