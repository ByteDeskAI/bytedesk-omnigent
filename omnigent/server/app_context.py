"""Typed server application context.

``create_app`` still exposes the historical ``app.state`` keys because route
handlers and tests use them as a compatibility surface. The primary owner of
those dependencies is now :class:`ServerAppContext`; binding to ``app.state`` is
just a projection step at the composition root.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


LEGACY_APP_STATE_KEYS: tuple[str, ...] = (
    "runner_control_registry",
    "runner_credential_store",
    "runner_router",
    "runner_exit_reports",
    "auth_provider",
    "assertion_signer",
    "agent_store",
    "file_store",
    "conversation_store",
    "artifact_store",
    "agent_cache",
    "comment_store",
    "policy_store",
    "permission_store",
    "runner_tunnel_tokens",
    "server_mcp_pool",
    "host_registry",
    "host_store",
    "sandbox_config",
    "managed_launches",
    "server_metrics",
    "server_metrics_otel",
    "di_container",
    "push_subscription_store",
    "session_liveness_lookup",
)
SERVER_APP_CONTEXT_STATE_KEY = "server_app_context"


@dataclass(frozen=True)
class ServerAppContext:
    """Typed owner of the app-wide server services built by ``create_app``."""

    runner_control_registry: Any
    runner_credential_store: Any | None
    runner_router: Any
    runner_exit_reports: Any
    auth_provider: Any | None
    assertion_signer: Any | None
    agent_store: Any
    file_store: Any
    conversation_store: Any
    artifact_store: Any
    agent_cache: Any
    comment_store: Any | None
    policy_store: Any | None
    permission_store: Any | None
    runner_tunnel_tokens: frozenset[str] | None
    server_mcp_pool: Any
    host_registry: Any
    host_store: Any | None
    sandbox_config: Any | None
    managed_launches: Any
    server_metrics: Any
    server_metrics_otel: Any
    di_container: Any
    push_subscription_store: Any
    session_liveness_lookup: Callable[[list[str]], dict[str, Any]]
    communication_services: Any


def bind_server_app_context(app: FastAPI, context: ServerAppContext) -> None:
    """Project ``context`` onto the existing ``app.state`` compatibility API."""
    setattr(app.state, SERVER_APP_CONTEXT_STATE_KEY, context)
    for key in LEGACY_APP_STATE_KEYS:
        setattr(app.state, key, getattr(context, key))


def get_server_app_context(app: FastAPI) -> ServerAppContext:
    """Return the typed server context installed on *app*.

    :raises RuntimeError: If the app was not built by
        :func:`omnigent.server.app.create_app`.
    """
    context = getattr(app.state, SERVER_APP_CONTEXT_STATE_KEY, None)
    if isinstance(context, ServerAppContext):
        return context
    raise RuntimeError("ServerAppContext is not installed on this FastAPI app")


__all__ = [
    "LEGACY_APP_STATE_KEYS",
    "SERVER_APP_CONTEXT_STATE_KEY",
    "ServerAppContext",
    "bind_server_app_context",
    "get_server_app_context",
]
