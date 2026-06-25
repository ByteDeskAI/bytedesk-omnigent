"""First-party plugin — ``omnigent.memory_maintenance`` (BDP-2509, Section 9.1/9.2/9.3).

Dogfoods the kernel's ``background_tasks`` seam: the periodic memory-maintenance
sweep (reinforcement flush + decay/eviction) that ``omnigent/server/app.py``'s
lifespan starts inline today is re-expressed here as a first-party plugin
contribution, registered through the *same* ``OmnigentExtension`` Protocol a
third-party extension uses. No new discovery, lifecycle, or registry mechanism —
the SDK ``@extension`` decorator compiles this class down to the kernel contract
(Section 12.7), and the existing :func:`omnigent.kernel.extensions.extension_background_factories`
aggregator collects what this plugin's ``background_tasks()`` returns.

The default provider — :func:`omnigent.runtime.memory_maintenance.memory_maintenance_loop`
— is **not** moved or rewritten; it is registered as-is through the seam hook.
Per Section 9.3 this plugin depends on ``omnigent.memory`` (the loop resolves the
memory store + reinforcement buffer at run time), so the loop coroutine is
imported lazily *inside* the hook to stay circular-import-safe and to keep
importing this module kernel-light.

NOT wired into boot here — the Integration phase mounts first-party plugins into
the lifespan DAG (Section 9.3). This module only needs to import cleanly and
expose the correct ``background_tasks()`` shape.
"""

from __future__ import annotations

from collections.abc import Awaitable

from ..sdk import background, extension


@extension(name="omnigent.memory_maintenance", requires=("omnigent.memory",))
class MemoryMaintenanceExtension:
    """First-party plugin contributing the memory-maintenance background loop.

    Registers exactly one ``background_tasks`` factory: the existing
    :func:`omnigent.runtime.memory_maintenance.memory_maintenance_loop`. The
    ``@background`` member decorator makes ``@extension`` synthesise the
    ``background_tasks() -> [factory() -> Awaitable[None]]`` Protocol hook;
    calling the factory returns the loop coroutine the server lifespan starts.
    """

    @background
    def memory_maintenance(self) -> Awaitable[None]:
        # Deferred import: ``memory_maintenance_loop`` resolves the memory store
        # and reinforcement buffer at run time (Section 9.3 dependency on
        # ``omnigent.memory``). Importing it here, not at module top, keeps this
        # plugin module kernel-light and circular-import-safe.
        from .memory_maintenance import memory_maintenance_loop

        return memory_maintenance_loop()


__all__ = ["MemoryMaintenanceExtension"]
