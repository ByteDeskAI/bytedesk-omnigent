"""ORM-level foreign-key enforcement for bdp2610schemaopt child tables."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from bytedesk_omnigent.db_models import (
    SqlConnectorAgentGrant,
    SqlConnectorConnection,
    SqlConnectorService,
    SqlGoal,
    SqlGoalDependency,
    SqlGoalOutcome,
    SqlInboundEvent,
    SqlInboundEventResult,
)
from omnigent.db.db_models import SqlComment, SqlConversation
from omnigent.db.utils import clear_engine_cache, get_or_create_engine, make_managed_session_maker


def _now() -> int:
    return int(time.time())


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "schema_opt_fk.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def _make_conversation(conv_id: str = "conv_fk_parent") -> SqlConversation:
    now = _now()
    return SqlConversation(
        id=conv_id,
        created_at=now,
        updated_at=now,
        kind="default",
        root_conversation_id=conv_id,
    )


def _make_goal(goal_id: str = "goal_fk_parent") -> SqlGoal:
    now = _now()
    return SqlGoal(
        id=goal_id,
        title="FK parent goal",
        created_at=now,
        updated_at=now,
    )


def _make_connector(connection_id: str = "conn_fk_parent") -> SqlConnectorConnection:
    now = _now()
    return SqlConnectorConnection(
        id=connection_id,
        provider="google",
        display_name="Test connector",
        auth_type="oauth_3lo",
        created_at=now,
        updated_at=now,
    )


def _make_inbound_event(key: str = "evt_fk_parent") -> SqlInboundEvent:
    now = _now()
    return SqlInboundEvent(
        idempotency_key=key,
        source="webhook",
        type="message.received",
        occurred_at=now,
        received_at=now,
        created_at=now,
        updated_at=now,
    )


class TestCommentsConversationFk:
    def test_accepts_existing_conversation(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with managed() as session:
            session.add(_make_conversation())
            session.flush()
            session.add(
                SqlComment(
                    id="cmt_ok",
                    conversation_id="conv_fk_parent",
                    path="src/a.ts",
                    start_index=0,
                    end_index=1,
                    body="ok",
                    status="draft",
                    created_at=now,
                    updated_at=now * 1_000_000,
                )
            )

        with managed() as session:
            assert session.get(SqlComment, "cmt_ok") is not None

    def test_rejects_missing_conversation(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(
                    SqlComment(
                        id="cmt_orphan",
                        conversation_id="conv_missing",
                        path="src/a.ts",
                        start_index=0,
                        end_index=1,
                        body="orphan",
                        status="draft",
                        created_at=now,
                        updated_at=now * 1_000_000,
                    )
                )


class TestGoalChildForeignKeys:
    def test_goal_dependency_accepts_parent(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with managed() as session:
            session.add(_make_goal())
            session.flush()
            session.add(
                SqlGoalDependency(
                    id="dep_ok",
                    goal_id="goal_fk_parent",
                    kind="manual",
                    label="Checklist item",
                    created_at=now,
                    updated_at=now,
                )
            )

        with managed() as session:
            assert session.get(SqlGoalDependency, "dep_ok") is not None

    def test_goal_dependency_rejects_orphan(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(
                    SqlGoalDependency(
                        id="dep_orphan",
                        goal_id="goal_missing",
                        kind="manual",
                        label="orphan",
                        created_at=now,
                        updated_at=now,
                    )
                )

    def test_goal_outcome_accepts_parent(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with managed() as session:
            session.add(_make_goal())
            session.flush()
            session.add(
                SqlGoalOutcome(
                    id="out_ok",
                    goal_id="goal_fk_parent",
                    booked_at=now,
                    realized_value_cents=100,
                    source="test",
                )
            )

        with managed() as session:
            assert session.get(SqlGoalOutcome, "out_ok") is not None

    def test_goal_outcome_rejects_orphan(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(
                    SqlGoalOutcome(
                        id="out_orphan",
                        goal_id="goal_missing",
                        booked_at=now,
                        realized_value_cents=50,
                        source="test",
                    )
                )


class TestInboundEventResultFk:
    def test_accepts_parent_event(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with managed() as session:
            session.add(_make_inbound_event())
            session.flush()
            session.add(
                SqlInboundEventResult(
                    id="ier_ok",
                    idempotency_key="evt_fk_parent",
                    processor="router",
                    created_at=now,
                    updated_at=now,
                )
            )

        with managed() as session:
            assert session.get(SqlInboundEventResult, "ier_ok") is not None

    def test_rejects_missing_event(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(
                    SqlInboundEventResult(
                        id="ier_orphan",
                        idempotency_key="evt_missing",
                        processor="router",
                        created_at=now,
                        updated_at=now,
                    )
                )


class TestConnectorChildForeignKeys:
    def test_service_accepts_connection(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with managed() as session:
            session.add(_make_connector())
            session.flush()
            session.add(
                SqlConnectorService(
                    id="svc_ok",
                    connection_id="conn_fk_parent",
                    service_key="gmail",
                    updated_at=now,
                )
            )

        with managed() as session:
            assert session.get(SqlConnectorService, "svc_ok") is not None

    def test_service_rejects_orphan_connection(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(
                    SqlConnectorService(
                        id="svc_orphan",
                        connection_id="conn_missing",
                        service_key="gmail",
                        updated_at=now,
                    )
                )

    def test_grant_accepts_connection(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with managed() as session:
            session.add(_make_connector())
            session.flush()
            session.add(
                SqlConnectorAgentGrant(
                    id="grant_ok",
                    connection_id="conn_fk_parent",
                    agent_id="ag_test",
                    service_key="gmail",
                    tool_key="send",
                    created_at=now,
                    updated_at=now,
                )
            )

        with managed() as session:
            assert session.get(SqlConnectorAgentGrant, "grant_ok") is not None

    def test_grant_rejects_orphan_connection(self, db_engine: Engine) -> None:
        managed = make_managed_session_maker(db_engine)
        now = _now()
        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(
                    SqlConnectorAgentGrant(
                        id="grant_orphan",
                        connection_id="conn_missing",
                        agent_id="ag_test",
                        service_key="gmail",
                        tool_key="send",
                        created_at=now,
                        updated_at=now,
                    )
                )