"""Batch-20 coverage for small omnigent/bytedesk_omnigent module gaps."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from sqlalchemy import update
from sqlalchemy.orm import Session

from bytedesk_omnigent.policies.budget import cost_hard_stop
from bytedesk_omnigent.policies.dry_run import dry_run_preview
from bytedesk_omnigent.realtime import config as realtime_config
from bytedesk_omnigent.session_state_store import SqlAlchemySessionStateStore
from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.coordination.replica_id import server_replica_id
from omnigent.coordination.sync import claim_resource, release_resource
from omnigent.entities.environment_filesystem import ResourceError
from omnigent.grok_native import _materialize_grok_agent_spec
from bytedesk_omnigent.policies.github import _ShellOp, github_policy
from omnigent.policies.builtins.prompt import prompt_policy
from omnigent.server import presence
from omnigent.server.routes.comments import create_comments_router
from omnigent.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE, labels_with_closed_status
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.db.db_models import SqlHost
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.host_store import HostStore, _parse_configured_harnesses
from omnigent.stores.lifecycle import LIFECYCLE_HOOKS_ENV_VAR, _invoke_one, run_store_lifecycle
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


# ── bytedesk_omnigent/realtime/config.py ─────────────────────────────────────


def test_redis_url_falls_back_to_canonical_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BYTEDESK_REDIS_URL is used when the realtime-specific override is unset."""
    monkeypatch.delenv("BYTEDESK_REALTIME_REDIS_URL", raising=False)
    monkeypatch.setenv("BYTEDESK_REDIS_URL", "redis://platform-redis:6379")
    assert realtime_config.redis_url() == "redis://platform-redis:6379"


# ── bytedesk_omnigent/session_state_store.py ─────────────────────────────────


def test_session_state_store_exposes_shared_engine(db_uri: str) -> None:
    """The facade exposes the underlying SQLAlchemy engine for shared wiring."""
    store = SqlAlchemySessionStateStore(db_uri)
    assert store.engine is store._engine
    assert store.engine.dialect.name == "sqlite"


# ── omnigent/grok_native.py ──────────────────────────────────────────────────


def test_materialize_grok_agent_spec_includes_model(tmp_path: Path) -> None:
    """An explicit model id is written into the generated executor block."""
    spec_path = _materialize_grok_agent_spec(tmp_path, model="grok-build")
    payload = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert payload["executor"]["model"] == "grok-build"


def test_materialize_grok_agent_spec_omits_model_when_unset(tmp_path: Path) -> None:
    """None lets the native CLI choose its default model."""
    spec_path = _materialize_grok_agent_spec(tmp_path, model=None)
    payload = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert "model" not in payload["executor"]


# ── bytedesk_omnigent/policies/github.py ─────────────────────────────────────


def test_github_policy_abstains_on_unclassified_shell_op_kind() -> None:
    """Shell ops outside read/write/unparseable abstain (return None)."""
    policy = github_policy()
    ignored = _ShellOp(
        kind="ignore",
        repo=None,
        branches=frozenset(),
        branch_targeted=False,
        detail="local git status",
    )
    with patch(
        "bytedesk_omnigent.policies.github._classify_shell_command",
        return_value=[ignored],
    ):
        result = policy(
            {
                "type": "tool_call",
                "data": {"name": "sys_os_shell", "arguments": {"command": "git status"}},
            }
        )
    assert result is None


# ── omnigent/policies/builtins/prompt.py ─────────────────────────────────────


class _TruthyEmptyReason:
    """Truthy sentinel that compares equal to an empty string."""

    def __bool__(self) -> bool:
        return True

    def __eq__(self, other: object) -> bool:
        return other == ""


@pytest.mark.asyncio
async def test_prompt_policy_normalizes_truthy_empty_reason_to_none() -> None:
    """Empty LLM reasons are normalized before building the response."""
    evaluate = prompt_policy(prompt="Deny empty reasons.")
    verdict = {"action": "deny", "reason": _TruthyEmptyReason()}
    client = AsyncMock()
    client.create.return_value = type("R", (), {"output_text": "{}"})()
    event = {
        "type": "request",
        "target": None,
        "data": "hello",
        "context": {},
        "session_state": {},
        "llm_client": client,
    }
    with patch("omnigent.policies.builtins.prompt.json.loads", return_value=verdict):
        result = await evaluate(event)
    assert result == {"result": "DENY", "reason": "Denied by prompt policy."}


# ── omnigent/server/presence.py ──────────────────────────────────────────────


def test_expire_leave_noops_when_viewer_already_gone() -> None:
    """Grace timer expiry is a no-op when the viewer entry was already removed."""
    presence.reset_for_tests()
    presence._expire_leave("conv_missing", "nobody@example.com")


# ── omnigent/server/routes/comments.py ───────────────────────────────────────


def test_create_comments_router_requires_conversation_store_with_permissions() -> None:
    """permission_store without conversation_store is rejected at router build."""
    comment_store = MagicMock()
    permission_store = MagicMock()
    with pytest.raises(
        ValueError,
        match="conversation_store is required when permission_store is provided",
    ):
        create_comments_router(
            comment_store,
            permission_store=permission_store,
            conversation_store=None,
        )


# ── omnigent/session_lifecycle.py ────────────────────────────────────────────


def test_labels_with_closed_status_adds_marker_from_title() -> None:
    """Legacy closed title markers synthesize the omnigent.closed label."""
    title = "researcher:auth:closed:conv_abc123"
    labels = labels_with_closed_status({"omnigent.wrapper": "codex-native-ui"}, title)
    assert labels == {
        "omnigent.wrapper": "codex-native-ui",
        CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE,
    }


def test_labels_with_closed_status_leaves_open_sessions_untouched() -> None:
    """Open sessions return labels unchanged."""
    original = {"omnigent.wrapper": "codex-native-ui"}
    assert labels_with_closed_status(original, "researcher:auth") == original


# ── omnigent/stores/artifact_store/__init__.py ───────────────────────────────


def test_artifact_store_storage_location_property(tmp_path: Path) -> None:
    """Concrete stores expose the backend-specific storage location."""
    location = str(tmp_path / "artifacts")
    store = LocalArtifactStore(location)
    assert store.storage_location == location


# ── omnigent/stores/host_store.py ────────────────────────────────────────────


def test_parse_configured_harnesses_rejects_non_object_json() -> None:
    """JSON arrays degrade to None instead of crashing host reads."""
    assert _parse_configured_harnesses('["claude-sdk"]') is None


def test_non_dict_configured_harnesses_column_reads_as_none(db_uri: str) -> None:
    """A JSON array stored in the column is tolerated at read time."""
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(
        host_id="host_array_harness",
        name="laptop-array",
        owner="alice@example.com",
        configured_harnesses={"codex": True},
    )
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlHost)
            .where(SqlHost.host_id == "host_array_harness")
            .values(configured_harnesses='["claude-sdk"]')
        )
        session.commit()

    fetched = host_store.get_host("host_array_harness")
    assert fetched is not None
    assert fetched.configured_harnesses is None


# ── omnigent/stores/lifecycle.py ─────────────────────────────────────────────


class _SyncLifecycleStore:
    """Store whose lifecycle hook returns a plain value (not awaitable)."""

    def startup(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_invoke_one_tolerates_sync_lifecycle_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync overrides are returned directly without awaiting."""
    monkeypatch.setenv(LIFECYCLE_HOOKS_ENV_VAR, "1")
    store = _SyncLifecycleStore()
    assert await _invoke_one(store, "startup") is True
    results = await run_store_lifecycle([store], "startup")
    assert results == {id(store): True}


# ── omnigent/stores/permission_store/sqlalchemy_store.py ─────────────────────


def test_ensure_user_uses_postgres_insert_on_non_sqlite_dialect(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-sqlite backends take the pg_insert upsert path."""
    store = SqlAlchemyPermissionStore(db_uri)
    monkeypatch.setattr(store._engine.dialect, "name", "postgresql")
    store.ensure_user("pg-user@example.com")
    assert store.is_admin("pg-user@example.com") is False


# ── omnigent/tools/__init__.py ───────────────────────────────────────────────


def test_omnigent_tools_lazy_export_and_unknown_attr() -> None:
    import omnigent.tools as tools

    assert tools.Tool is not None
    with pytest.raises(AttributeError, match="has no attribute"):
        _ = tools.NotARealExportName


# ── bytedesk_omnigent/policies/budget.py ─────────────────────────────────────


def test_cost_hard_stop_treats_unparseable_spend_as_zero() -> None:
    breaker = cost_hard_stop(max_cost_usd=1.0)
    event: dict[str, Any] = {
        "type": "request",
        "context": {"usage": {"total_cost_usd": "not-a-number"}},
    }
    assert breaker(event)["result"] == "ALLOW"


# ── bytedesk_omnigent/policies/dry_run.py ────────────────────────────────────


def test_dry_run_preview_falls_back_to_str_on_circular_arguments() -> None:
    evaluate = dry_run_preview(["wipe\\.all"])
    circular: dict[str, Any] = {}
    circular["self"] = circular
    result = evaluate(
        {
            "type": "tool_call",
            "data": {"name": "wipe.all", "arguments": circular},
        }
    )
    assert result["result"] == "ASK"
    assert "wipe.all" in result["reason"]


# ── bytedesk_omnigent/secrets/__init__.py ────────────────────────────────────


def test_bytedesk_secrets_package_reexports() -> None:
    from bytedesk_omnigent.secrets import InfisicalBackend

    assert InfisicalBackend is not None


# ── omnigent/coordination/inprocess.py ───────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_drops_payload_when_subscriber_queue_is_full() -> None:
    """A full subscriber queue must not block publishers."""
    bp = InProcessBackplane("replica-a")
    await bp.start()
    full_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
    full_queue.put_nowait(b"occupied")
    bp._pub_sub["omnigent.coord.full"] = [full_queue]
    await bp.publish("omnigent.coord.full", b"overflow")
    assert full_queue.qsize() == 1
    assert full_queue.get_nowait() == b"occupied"
    await bp.stop()


# ── omnigent/coordination/replica_id.py ──────────────────────────────────────


def test_server_replica_id_prefers_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_REPLICA_ID", "  replica-explicit  ")
    assert server_replica_id() == "replica-explicit"


def test_server_replica_id_uses_kubernetes_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_REPLICA_ID", raising=False)
    monkeypatch.setenv("HOSTNAME", "omnigent-server-7f3c9")
    assert server_replica_id() == "omnigent-server-7f3c9"


def test_server_replica_id_falls_back_to_local_suffix_when_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_REPLICA_ID", raising=False)
    monkeypatch.setenv("HOSTNAME", "localhost")
    replica = server_replica_id()
    assert replica.startswith("local-")


# ── omnigent/coordination/sync.py ────────────────────────────────────────────


def test_coordination_sync_noops_when_backplane_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claim/release return immediately when no backplane is active."""
    monkeypatch.setattr(
        "omnigent.coordination.lifecycle.get_active_backplane",
        lambda: None,
    )
    claim_resource("runner", "runner_absent")
    release_resource("runner", "runner_absent")


def test_coordination_sync_schedules_claim_and_release_on_active_backplane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claim/release forward to schedule_backplane when a backplane is live."""
    scheduled: list[str] = []
    backplane = MagicMock()
    backplane.claim_resource = MagicMock(return_value="claim-coro")
    backplane.release_resource = MagicMock(return_value="release-coro")

    monkeypatch.setattr(
        "omnigent.coordination.lifecycle.get_active_backplane",
        lambda: backplane,
    )
    monkeypatch.setattr(
        "omnigent.coordination.sync.schedule_backplane",
        lambda coro: scheduled.append(str(coro)),
    )
    claim_resource("runner", "runner_batch20")
    release_resource("runner", "runner_batch20")
    assert scheduled == ["claim-coro", "release-coro"]


# ── omnigent/entities/environment_filesystem.py ─────────────────────────────


def test_resource_error_sets_message_attribute() -> None:
    err = ResourceError("filesystem failure")
    assert err.message == "filesystem failure"
    assert str(err) == "filesystem failure"