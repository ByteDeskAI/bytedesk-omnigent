"""First-party plugin for the ``omnigent.spec`` subpackage (BDP-2509).

Section 9.1 of ``docs/EXTENSION_FRAMEWORK_ANALYSIS.md`` lists this subpackage as
the ``omnigent.spec`` first-party plugin: it registers the package's *existing*
default :class:`~omnigent.spec.source.SpecSource` into the kernel ``spec_source``
seam. Per the dogfooding argument (Section 9.2), a core capability ships through
the *same* :class:`omnigent.extensions.OmnigentExtension` / ``PluggableRegistry``
contract a third-party extension would use — there is no privileged wiring for
core's own spec source. If the seam can host this default, it can host an
alternate (DB / URL / OCI) source too.

This module **does not** move, rewrite, or re-implement any provider. The
:class:`~omnigent.spec.source.FilesystemSpecSource` and the
:data:`~omnigent.spec.source.spec_source_registry` stay exactly where they are;
this plugin only *registers* the existing default through the seam's
``spec_source_providers`` extension hook — the same hook a future external spec
source would contribute through.

Why a hand-written ``spec_source_providers`` rather than an SDK member
decorator: the ``spec_source`` seam is a generic :mod:`omnigent.pluggable`
``PluggableRegistry`` seam discovered via the
:data:`~omnigent.spec.source.EXTENSION_HOOK` (``"spec_source_providers"``), not
one of the SDK's curated member-decorator seams (``@tool`` / ``@policy`` /
``@harness`` / ``@background`` / ``@router`` / ``@tool_interceptor``). The SDK's
:func:`omnigent.sdk.extension` only synthesises a hook it does not already find
on the class, so an author-written ``spec_source_providers`` is preserved
verbatim — the decorator still stamps ``name``/``requires`` and fills the
remaining optional Protocol members so the instance satisfies
``isinstance(obj, OmnigentExtension)``.

Heavy / domain imports (the ``omnigent.spec.source`` module, which pulls in the
spec parser) are deferred *inside* the hook method so importing this plugin
stays kernel-light and circular-import-safe — the same deferred-import
discipline the kernel's own ``discover_extensions`` proxy and the bytedesk
identity-port hooks follow.

NOTE: This plugin is **not yet wired into boot** — the Integration phase mounts
it. Here it only needs to import cleanly and expose correct hook returns.
"""

from __future__ import annotations

from collections.abc import Callable

from omnigent.sdk import extension


@extension(name="omnigent.spec")
class SpecPlugin:
    """First-party ``omnigent.spec`` plugin — registers the default spec source.

    Registers this subpackage's existing :class:`FilesystemSpecSource` into the
    kernel ``spec_source`` :class:`~omnigent.pluggable.PluggableRegistry` seam,
    under the same name (``"filesystem"``) the registry already uses for its
    built-in default (see
    :func:`~omnigent.spec.source.build_spec_source_registry`). The contribution
    is therefore the seam-expressed form of the existing default — dogfooding,
    not a behaviour change.
    """

    def spec_source_providers(self) -> dict[str, Callable[[], object]]:
        """Contribute the default :class:`SpecSource` to the ``spec_source`` seam.

        Returns the ``{name: factory}`` mapping the ``spec_source`` seam's
        :meth:`PluggableRegistry.discover_extensions` expects: each value is a
        zero-argument factory the registry calls (via
        :meth:`~omnigent.pluggable.PluggableRegistry.get`) to build the
        :class:`~omnigent.spec.source.SpecSource` on demand.

        The provider class is imported lazily here (not at module import) to keep
        this plugin importable without dragging in the spec parser, and to stay
        circular-import-safe — the kernel resolves the factory only when a spec
        source is actually requested.
        """
        from omnigent.spec.source import FilesystemSpecSource

        # ``FilesystemSpecSource()`` with no scan root mirrors the module-level
        # ``spec_source_registry`` default (``build_spec_source_registry()`` with
        # ``root=None``). The factory is zero-arg because the seam's ``get(name)``
        # calls ``factory()`` to instantiate.
        return {"filesystem": lambda: FilesystemSpecSource()}


__all__ = ["SpecPlugin"]
