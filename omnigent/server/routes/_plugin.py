"""First-party ``omnigent.routes`` plugin (BDP-2509, Section 9.1 "routes" row).

Dogfoods the kernel seam: the HTTP route factories that live in
``omnigent/server/routes/`` are mounted today *directly* inside
``create_app()`` (``app.include_router(create_*_router(...))``). Section 9.2
argues the framework's own routes should flow through the *same*
``OmnigentExtension.routers()`` seam a third-party extension uses — if the seam
can host every first-party router it can host anyone's, and core gains no
privileged route set.

This module is the first step of that migration: it declares a first-party
:class:`RoutesPlugin` via the :mod:`omnigent.sdk` ``@extension`` facade whose
``@router`` methods return this subpackage's **existing** concrete router
factories. It does **not** move or rewrite any factory — it only re-registers
them through the seam (the providers stay exactly where they are in
``policy_registry.py`` et al.).

Scope of *this* phase (Section 9.3 boot order shows ``routes plugin registers``
runs late, after every store is built): the plugin must

  * import cleanly with the kernel only (no store construction at import), and
  * expose a correct, non-empty ``routers()`` return.

Only the **dependency-free** router is wired live here: ``policy_registry``
needs nothing but the ``auth_provider`` the seam already forwards, so it is a
real, self-contained contribution that proves the seam end-to-end. The
remaining route factories in this subpackage (``sessions``, ``data_surfaces``,
``builtin_agents``, ``agents_write``, ``skills``, ``terminal_attach``,
``comments``, ``session_policies``, ``default_policies``) each require concrete
stores (``conversation_store``, ``agent_store``, ``file_store``,
``artifact_store``, ``comment_store``, ``policy_store``, …) that only exist
*inside* ``create_app()``. The seam's synthesised ``routers()`` forwards only
``auth_provider`` / ``permission_store``, so those store-backed factories are
left to the Integration phase, which will thread the built stores into this
plugin (e.g. via DI ``@provides`` on the host container). They are enumerated in
:data:`STORE_BACKED_ROUTE_FACTORIES` as documentation of the remaining surface.

This plugin is **not** wired into boot here — ``create_app()`` keeps mounting
the factories inline (the non-negotiable back-compat rule). Wiring it into the
``install_extensions()`` path, and removing the now-duplicated inline mounts, is
the Integration phase's job.

Every heavy / FastAPI import is deferred inside the hook bodies so importing
this module stays kernel-light and circular-import-safe (the routes modules pull
in the whole store + FastAPI stack).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.sdk import extension, router

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import APIRouter

    from omnigent.server.auth import AuthProvider


#: The store-backed route factories in this subpackage that this plugin will own
#: once the Integration phase threads the built stores into it. Documented here
#: (not wired) so the remaining surface is explicit; the dotted paths resolve to
#: the *existing* factories — nothing is moved. Each tuple is
#: ``(module, factory, required_stores)``.
STORE_BACKED_ROUTE_FACTORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "omnigent.server.routes.sessions",
        "create_sessions_router",
        ("conversation_store", "agent_store", "file_store", "artifact_store"),
    ),
    (
        "omnigent.server.routes.data_surfaces",
        "create_data_surfaces_router",
        ("conversation_store",),
    ),
    (
        "omnigent.server.routes.builtin_agents",
        "create_builtin_agents_router",
        ("agent_store", "agent_cache"),
    ),
    (
        "omnigent.server.routes.agents_write",
        "create_agents_write_router",
        ("agent_store", "agent_cache", "artifact_store"),
    ),
    (
        "omnigent.server.routes.skills",
        "create_skills_router",
        ("agent_store", "agent_cache", "artifact_store"),
    ),
    (
        "omnigent.server.routes.terminal_attach",
        "create_terminal_attach_router",
        ("conversation_store",),
    ),
    (
        "omnigent.server.routes.comments",
        "create_comments_router",
        ("comment_store", "conversation_store"),
    ),
    (
        "omnigent.server.routes.session_policies",
        "create_session_policies_router",
        ("policy_store", "conversation_store"),
    ),
    (
        "omnigent.server.routes.default_policies",
        "create_default_policies_router",
        ("policy_store",),
    ),
)


@extension(name="omnigent.routes")
class RoutesPlugin:
    """First-party plugin contributing ``omnigent/server/routes/`` factories.

    A plain class the :func:`omnigent.sdk.extension` decorator compiles down to
    an :class:`omnigent.extensions.OmnigentExtension`: the ``@router`` method
    below is gathered into a synthesised
    ``routers(auth_provider=..., permission_store=...)`` that returns the same
    ``list[APIRouter]`` shape ``create_app()`` mounts today.
    """

    @router()
    def policy_registry_router(
        self,
        auth_provider: "AuthProvider | None" = None,
        permission_store: object | None = None,
    ) -> "APIRouter":
        """Contribute the dependency-free ``/policy-registry`` router.

        Mirrors the inline ``app.include_router(create_policy_registry_router(
        auth_provider=auth_provider))`` mount in ``create_app()``. Imported
        lazily so this module imports without pulling FastAPI / the routes
        stack — and so it is safe against circular imports (the routes package
        imports the store + identity stack this plugin lives alongside).

        ``permission_store`` is accepted (the seam forwards it) but unused: this
        factory authorizes purely on ``auth_provider`` presence, identical to
        the current inline mount.
        """
        from omnigent.server.routes.policy_registry import (
            create_policy_registry_router,
        )

        return create_policy_registry_router(auth_provider=auth_provider)


__all__ = ["RoutesPlugin", "STORE_BACKED_ROUTE_FACTORIES"]
