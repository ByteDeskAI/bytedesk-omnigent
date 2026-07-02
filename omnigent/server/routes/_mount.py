"""Typed first-party route mounting for the omnigent server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.server.app_context import ServerAppContext

if TYPE_CHECKING:
    from fastapi import FastAPI


@dataclass(frozen=True)
class RouteMountContext:
    """Dependencies required to mount first-party store-backed routes."""

    conversation_store: Any
    agent_store: Any
    file_store: Any
    artifact_store: Any
    runner_router: Any
    auth_provider: Any | None
    permission_store: Any | None
    agent_cache: Any
    server_mcp_pool: Any
    session_liveness_lookup: Any
    comment_store: Any | None
    runner_tunnel_tokens: frozenset[str] | None
    runner_exit_reports: Any
    runner_control_registry: Any
    host_store: Any | None
    policy_store: Any | None
    push_subscription_store: Any

    @classmethod
    def from_server_context(cls, context: ServerAppContext) -> RouteMountContext:
        """Build the route-mount view from the broader server context."""
        return cls(
            conversation_store=context.conversation_store,
            agent_store=context.agent_store,
            file_store=context.file_store,
            artifact_store=context.artifact_store,
            runner_router=context.runner_router,
            auth_provider=context.auth_provider,
            permission_store=context.permission_store,
            agent_cache=context.agent_cache,
            server_mcp_pool=context.server_mcp_pool,
            session_liveness_lookup=context.session_liveness_lookup,
            comment_store=context.comment_store,
            runner_tunnel_tokens=context.runner_tunnel_tokens,
            runner_exit_reports=context.runner_exit_reports,
            runner_control_registry=context.runner_control_registry,
            host_store=context.host_store,
            policy_store=context.policy_store,
            push_subscription_store=context.push_subscription_store,
        )


def mount_store_backed_routes(host: FastAPI, context: RouteMountContext) -> None:
    """Mount first-party store-backed routes using a typed dependency context."""
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
            context.conversation_store,
            context.agent_store,
            file_store=context.file_store,
            artifact_store=context.artifact_store,
            runner_router=context.runner_router,
            auth_provider=context.auth_provider,
            permission_store=context.permission_store,
            agent_cache=context.agent_cache,
            mcp_pool=context.server_mcp_pool,
            liveness_lookup=context.session_liveness_lookup,
            comment_store=context.comment_store,
            runner_tunnel_tokens=context.runner_tunnel_tokens,
            runner_exit_reports=context.runner_exit_reports,
        ),
        prefix="/v1",
        tags=["sessions"],
    )
    host.include_router(
        create_runners_router(
            context.runner_control_registry,
            auth_provider=context.auth_provider,
            runner_exit_reports=context.runner_exit_reports,
        ),
        prefix="/v1",
        tags=["runners"],
    )
    host.include_router(
        create_data_surfaces_router(
            context.conversation_store,
            auth_provider=context.auth_provider,
            permission_store=context.permission_store,
            host_store=context.host_store,
        ),
        prefix="/v1",
        tags=["data-surfaces"],
    )
    host.include_router(
        create_builtin_agents_router(
            context.agent_store,
            context.agent_cache,
            auth_provider=context.auth_provider,
        ),
        prefix="/v1",
        tags=["agents"],
    )
    host.include_router(
        create_agents_write_router(
            context.agent_store,
            context.agent_cache,
            context.artifact_store,
            auth_provider=context.auth_provider,
            permission_store=context.permission_store,
        ),
        prefix="/v1",
        tags=["agents"],
    )
    host.include_router(
        create_skills_router(
            context.agent_store,
            context.agent_cache,
            context.artifact_store,
            auth_provider=context.auth_provider,
            conversation_store=context.conversation_store,
            runner_router=context.runner_router,
            permission_store=context.permission_store,
        ),
        prefix="/v1",
        tags=["skills"],
    )
    host.include_router(
        create_terminal_attach_router(
            auth_provider=context.auth_provider,
            permission_store=context.permission_store,
            conversation_store=context.conversation_store,
        ),
        prefix="/v1",
        tags=["terminals"],
    )
    if context.comment_store is not None:
        host.include_router(
            create_comments_router(
                context.comment_store,
                auth_provider=context.auth_provider,
                permission_store=context.permission_store,
                conversation_store=context.conversation_store,
            ),
            prefix="/v1",
            tags=["comments"],
        )
    if context.policy_store is not None:
        host.include_router(
            create_session_policies_router(
                context.policy_store,
                context.conversation_store,
                auth_provider=context.auth_provider,
                permission_store=context.permission_store,
            ),
            prefix="/v1",
            tags=["session_policies"],
        )
        host.include_router(
            create_default_policies_router(
                context.policy_store,
                auth_provider=context.auth_provider,
                permission_store=context.permission_store,
            ),
            prefix="/v1",
            tags=["default_policies"],
        )
    host.include_router(
        create_policy_registry_router(auth_provider=context.auth_provider),
        prefix="/v1",
        tags=["policy_registry"],
    )
    host.include_router(
        create_push_router(context.push_subscription_store, context.auth_provider),
        prefix="/v1",
        tags=["push"],
    )


__all__ = ["RouteMountContext", "mount_store_backed_routes"]
