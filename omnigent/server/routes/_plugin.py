"""First-party ``omnigent.routes`` plugin (BDP-2509, Section 9.1 "routes" row).

Dogfoods the kernel seam: the HTTP route factories that live in
``omnigent/server/routes/`` are mounted today *directly* inside
``create_app()`` (``app.include_router(create_*_router(...))``). Section 9.2
argues the framework's own routes should flow through the *same*
``OmnigentExtension.routers()`` seam a third-party extension uses — if the seam
can host every first-party router it can host anyone's, and core gains no
privileged route set.

This module declares a first-party :class:`RoutesExtension` whose ``post_init``
hook mounts this subpackage's **existing** concrete router factories from the
typed server app context built by ``create_app()``. It does **not** move or
rewrite any factory — it only registers them through the same extension
lifecycle the third-party route seam already uses.

Scope of this slice: the plugin must import cleanly with the kernel only (no
store construction at import) and mount the documented store-backed route group
after the app host has built its stores. Route factories outside this subpackage
(hosts, accounts/auth, SPA/static, caller-provided ``extra_routers``) remain in
``create_app()`` because they are separate
composition-root concerns.

Every heavy / FastAPI import is deferred inside the hook bodies so importing
this module stays kernel-light and circular-import-safe (the routes modules pull
in the whole store + FastAPI stack).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.sdk import extension

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import FastAPI


#: The store-backed route factories in this subpackage owned by this plugin.
#: Documented as ``(module, factory, required_state_keys)`` so the cutover has a
#: stable, reviewable route inventory in one place.
STORE_BACKED_ROUTE_FACTORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "omnigent.server.routes.sessions",
        "create_sessions_router",
        (
            "conversation_store",
            "agent_store",
            "file_store",
            "artifact_store",
            "runner_router",
            "agent_cache",
            "server_mcp_pool",
            "session_liveness_lookup",
            "comment_store",
            "runner_tunnel_tokens",
            "runner_exit_reports",
        ),
    ),
    (
        "omnigent.server.routes.runners",
        "create_runners_router",
        ("runner_control_registry", "runner_exit_reports"),
    ),
    (
        "omnigent.server.routes.data_surfaces",
        "create_data_surfaces_router",
        ("conversation_store", "host_store"),
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
        (
            "agent_store",
            "agent_cache",
            "artifact_store",
            "conversation_store",
            "runner_router",
        ),
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
    (
        "omnigent.server.routes.policy_registry",
        "create_policy_registry_router",
        (),
    ),
    (
        "omnigent.server.routes.push",
        "create_push_router",
        ("push_subscription_store",),
    ),
)


@extension(name="omnigent.routes")
class RoutesExtension:
    """First-party plugin contributing ``omnigent/server/routes/`` factories.

    A plain class the :func:`omnigent.sdk.extension` decorator compiles down to
    an :class:`omnigent.kernel.extensions.OmnigentExtension`. The synthesized
    ``routers()`` method returns ``[]``; the store-backed route group mounts in
    ``post_init`` after ``create_app()`` has installed
    :class:`~omnigent.server.app_context.ServerAppContext`.
    """

    def post_init(self, host: FastAPI) -> None:
        """Mount the first-party core route group from the app-host context."""
        from omnigent.server.app_context import get_server_app_context
        from omnigent.server.routes._mount import (
            RouteMountContext,
            mount_store_backed_routes,
        )

        mount_store_backed_routes(
            host,
            RouteMountContext.from_server_context(get_server_app_context(host)),
        )


__all__ = ["STORE_BACKED_ROUTE_FACTORIES", "RoutesExtension"]
