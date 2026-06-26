"""Regression tests for first-party core route cutover (BDP-2517)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore


def _build_route_app(db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        policy_store=SqlAlchemyPolicyStore(db_uri),
    )


def _route_entries(app: FastAPI) -> set[tuple[str, str, str]]:
    entries: set[tuple[str, str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        tags = getattr(route, "tags", None) or []
        if path is None or methods is None:
            continue
        tag = str(tags[0]) if tags else ""
        for method in methods:
            entries.add((method, path, tag))
    return entries


def test_firstparty_routes_mount_without_legacy_flag(
    db_uri: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Core route group is installed by ``RoutesExtension`` without the old flag."""
    monkeypatch.delenv("OMNIGENT_USE_FIRSTPARTY_PLUGINS", raising=False)
    monkeypatch.setenv("OMNIGENT_DISABLED_EXTENSIONS", "bytedesk")

    app = _build_route_app(db_uri, tmp_path)

    for key in {
        "agent_store",
        "file_store",
        "conversation_store",
        "artifact_store",
        "agent_cache",
        "comment_store",
        "policy_store",
        "permission_store",
        "runner_tunnel_tokens",
        "runner_exit_reports",
        "server_mcp_pool",
        "session_liveness_lookup",
        "push_subscription_store",
    }:
        assert hasattr(app.state, key), key

    entries = _route_entries(app)
    assert {
        ("POST", "/v1/sessions", "sessions"),
        ("GET", "/v1/sessions/{session_id}/memories", "data-surfaces"),
        ("GET", "/v1/agents", "agents"),
        ("PUT", "/v1/agents/{agent_id}/image", "agents"),
        ("GET", "/v1/skills/marketplaces", "skills"),
        ("POST", "/v1/sessions/{session_id}/comments", "comments"),
        ("POST", "/v1/sessions/{session_id}/policies", "session_policies"),
        ("GET", "/v1/policies", "default_policies"),
        ("GET", "/v1/policy-registry", "policy_registry"),
        ("GET", "/v1/push/vapid-public-key", "push"),
    } <= entries
