"""First-party plugin for the ``omnigent/tools/builtins`` subpackage (BDP-2509).

Per Section 9.1 (the ``tools`` row) and Section 9.2 (the dogfooding argument) of
``docs/EXTENSION_FRAMEWORK_ANALYSIS.md``, the built-in tool set is a *first-party
plugin* contribution into the ``tools`` kernel seam — not a privileged hard-coded
default. This module expresses that: it wraps the subpackage's **already-existing**
default tool factories (``_FIRST_PARTY_FACTORIES`` in
:mod:`omnigent.tools.builtins`) as an :class:`~omnigent.kernel.extensions.OmnigentExtension`
via the :func:`omnigent.sdk.extension` decorator, registering them through the same
``tool_factories`` hook a third-party extension uses.

Dogfooding (Section 9.2): if the ``tool_factories`` seam can host the full first-party
builtin set, it can host a third-party tool too. There is no separate "how core adds
tools" vs "how extensions add tools" — both flow through this one hook.

**Reuse, don't duplicate (Section 9.1 note):** the concrete provider factories are NOT
moved or rewritten here. ``tool_factories()`` returns the *existing*
``_FIRST_PARTY_FACTORIES`` mapping verbatim — imported lazily inside the hook so this
module stays circular-import-safe and kernel-light (importing it pulls in neither the
heavy tool implementations nor the FastAPI stack until the hook actually fires).

This plugin is **not** wired into boot yet — the Integration phase does that. It only
needs to import cleanly and expose the correct hook returns. Because the SDK only
synthesises a hook the author did *not* hand-write, defining ``tool_factories`` by hand
(to reuse the existing config-accepting factories) is honoured; the SDK still fills the
required ``routers()`` and the remaining optional Protocol members with behaviour-neutral
empty defaults, so an instance satisfies ``isinstance(obj, OmnigentExtension)``.
"""

from __future__ import annotations

from collections.abc import Callable

from omnigent.sdk import extension

#: This plugin's kernel-seam name. Matches the Section 9.1 ``omnigent.tools`` row.
_PLUGIN_NAME = "omnigent.tools"


@extension(name=_PLUGIN_NAME)
class BuiltinToolsExtension:
    """First-party plugin contributing the built-in tool set to the ``tools`` seam.

    The single capability hook is ``tool_factories()`` — the same hook the kernel's
    ``tools`` :class:`~omnigent.kernel.pluggable.PluggableRegistry` seam and the legacy
    ``extension_tool_factories()`` aggregator already consume. The SDK synthesises the
    required ``routers()`` (returning ``[]``) and the remaining optional Protocol hooks
    as behaviour-neutral no-ops, so the instance conforms to ``OmnigentExtension``
    without contributing into any other seam.
    """

    def tool_factories(self) -> dict[str, Callable[[object], object]]:
        """Return the subpackage's existing default tool factories.

        Reuses the concrete ``_FIRST_PARTY_FACTORIES`` mapping defined in
        :mod:`omnigent.tools.builtins` rather than re-declaring it — the dogfooding
        contribution is *registration*, not duplication. Imported lazily (inside the
        hook, not at module top level) to avoid the import cycle that pulling the tool
        implementations in at import time would create, and to keep this module
        kernel-light.

        :returns: ``{tool_name: factory(config) -> Tool}`` — the exact shape the
            ``tools`` seam expects; a shallow copy so the caller can never mutate the
            subpackage's source-of-truth mapping.
        """
        from omnigent.tools.builtins import _FIRST_PARTY_FACTORIES

        return dict(_FIRST_PARTY_FACTORIES)


#: The plugin instance the Integration phase will register through the kernel's
#: ``discover_extensions`` entry-point / env-var seam (not wired here yet). Exposed as
#: a module-level singleton so a ``module:factory`` entry-point can resolve it without
#: re-instantiating.
def build_extension() -> BuiltinToolsExtension:
    """Factory for the entry-point/env-var discovery seam (used at Integration time)."""
    return BuiltinToolsExtension()


__all__ = ["BuiltinToolsExtension", "build_extension"]
