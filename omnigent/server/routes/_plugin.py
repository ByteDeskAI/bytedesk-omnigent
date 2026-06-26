"""First-party ``omnigent.routes`` plugin (BDP-2509, Section 9.1 "routes" row).

Dogfoods the kernel seam: the HTTP route factories that live in
``omnigent/server/routes/`` are mounted today *directly* inside
``create_app()`` (``app.include_router(create_*_router(...))``). Section 9.2
argues the framework's own routes should flow through the *same*
``OmnigentExtension.routers()`` seam a third-party extension uses — if the seam
can host every first-party router it can host anyone's, and core gains no
privileged route set.

This module declares a first-party :class:`RoutesExtension` whose ``post_init``
hook mounts this subpackage's **existing** concrete router factories using the
stores already built by ``create_app()`` and exposed on ``app.state``. It does
**not** move or rewrite any factory — it only registers them through the same
extension lifecycle the third-party route seam already uses.

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
    ``post_init`` after ``create_app()`` has exposed the built stores on
    ``app.state``.
    """

    def post_init(self, host: FastAPI) -> None:
        """Mount the first-party core route group from the app-host context."""
        state = host.state
        auth_provider = getattr(state, "auth_provider", None)
        permission_store = getattr(state, "permission_store", None)

        from omnigent.server.routes.agents_write import create_agents_write_router
        from omnigent.server.routes.builtin_agents import create_builtin_agents_router
        from omnigent.server.routes.comments import create_comments_router
        from omnigent.server.routes.data_surfaces import create_data_surfaces_router
        from omnigent.server.routes.default_policies import create_default_policies_router
        from omnigent.server.routes.policy_registry import create_policy_registry_router
        from omnigent.server.routes.push import create_push_router
        from omnigent.server.routes.runners import create_runners_router
        from omnigent.server.routes.session_policies import create_session_policies_router
        from omnigent.server.routes.sessions import create_sessions_router
        from omnigent.server.routes.skills import create_skills_router
        from omnigent.server.routes.terminal_attach import create_terminal_attach_router

        host.include_router(
            create_sessions_router(
                state.conversation_store,
                state.agent_store,
                file_store=state.file_store,
                artifact_store=state.artifact_store,
                runner_router=state.runner_router,
                auth_provider=auth_provider,
                permission_store=permission_store,
                agent_cache=state.agent_cache,
                mcp_pool=state.server_mcp_pool,
                liveness_lookup=state.session_liveness_lookup,
                comment_store=state.comment_store,
                runner_tunnel_tokens=state.runner_tunnel_tokens,
                runner_exit_reports=state.runner_exit_reports,
            ),
            prefix="/v1",
            tags=["sessions"],
        )
        host.include_router(
            create_runners_router(
                state.runner_control_registry,
                auth_provider=auth_provider,
                runner_exit_reports=state.runner_exit_reports,
            ),
            prefix="/v1",
            tags=["runners"],
        )
        host.include_router(
            create_data_surfaces_router(
                state.conversation_store,
                auth_provider=auth_provider,
                permission_store=permission_store,
                host_store=state.host_store,
            ),
            prefix="/v1",
            tags=["data-surfaces"],
        )
        host.include_router(
            create_builtin_agents_router(
                state.agent_store,
                state.agent_cache,
                auth_provider=auth_provider,
            ),
            prefix="/v1",
            tags=["agents"],
        )
        host.include_router(
            create_agents_write_router(
                state.agent_store,
                state.agent_cache,
                state.artifact_store,
                auth_provider=auth_provider,
            ),
            prefix="/v1",
            tags=["agents"],
        )
        host.include_router(
            create_skills_router(
                state.agent_store,
                state.agent_cache,
                state.artifact_store,
                auth_provider=auth_provider,
                conversation_store=state.conversation_store,
                runner_router=state.runner_router,
                permission_store=permission_store,
            ),
            prefix="/v1",
            tags=["skills"],
        )
        host.include_router(
            create_terminal_attach_router(
                auth_provider=auth_provider,
                permission_store=permission_store,
                conversation_store=state.conversation_store,
            ),
            prefix="/v1",
            tags=["terminals"],
        )
        if state.comment_store is not None:
            host.include_router(
                create_comments_router(
                    state.comment_store,
                    auth_provider=auth_provider,
                    permission_store=permission_store,
                    conversation_store=state.conversation_store,
                ),
                prefix="/v1",
                tags=["comments"],
            )
        if state.policy_store is not None:
            host.include_router(
                create_session_policies_router(
                    state.policy_store,
                    state.conversation_store,
                    auth_provider=auth_provider,
                    permission_store=permission_store,
                ),
                prefix="/v1",
                tags=["session_policies"],
            )
            host.include_router(
                create_default_policies_router(
                    state.policy_store,
                    auth_provider=auth_provider,
                    permission_store=permission_store,
                ),
                prefix="/v1",
                tags=["default_policies"],
            )
        host.include_router(
            create_policy_registry_router(auth_provider=auth_provider),
            prefix="/v1",
            tags=["policy_registry"],
        )
        host.include_router(
            create_push_router(state.push_subscription_store, auth_provider),
            prefix="/v1",
            tags=["push"],
        )


__all__ = ["STORE_BACKED_ROUTE_FACTORIES", "RoutesExtension"]
