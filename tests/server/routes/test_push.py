"""Tests for Web Push subscription routes and attention dispatcher."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.db.db_models import SqlPushSubscription
from omnigent.server.push.attention import should_notify_new_elicitation, should_notify_turn_end
from omnigent.server.push.sender import build_push_payload
from omnigent.server.push.service import PushNotificationService
from omnigent.server.push.vapid import generate_ephemeral_vapid_keys
from omnigent.server.routes.push import create_push_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.push_subscription_store.sqlalchemy_store import (
    SqlAlchemyPushSubscriptionStore,
    _upsert_insert_for_engine,
)


def test_attention_predicates_match_idle_transitions() -> None:
    assert should_notify_turn_end("running", "idle") is True
    assert should_notify_turn_end("idle", "idle") is False
    assert should_notify_new_elicitation(0, 1) is True
    assert should_notify_new_elicitation(None, 1) is False


def test_push_payload_shape() -> None:
    payload = build_push_payload(
        session_id="conv_test",
        title="Session",
        body="Ready",
        kind="turn_end",
    )
    assert payload["sessionId"] == "conv_test"
    assert payload["url"] == "/c/conv_test"


@pytest.fixture
def push_client(db_uri: str) -> TestClient:
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    sub_store = SqlAlchemyPushSubscriptionStore(db_uri)
    vapid = generate_ephemeral_vapid_keys()
    service = PushNotificationService(
        subscription_store=sub_store,
        permission_store=perm_store,
        conversation_store=conv_store,
        vapid=vapid,
    )

    class _HeaderAuth:
        def get_user_id(self, request: object) -> str:
            del request
            return "alice@example.com"

    app = FastAPI()
    app.include_router(create_push_router(sub_store, _HeaderAuth()), prefix="/v1")

    from omnigent.server.push import service as push_service_module

    push_service_module.set_push_service(service)
    return TestClient(app)


def test_subscription_crud(push_client: TestClient) -> None:
    key_res = push_client.get("/v1/push/vapid-public-key")
    assert key_res.status_code == 200
    assert "public_key" in key_res.json()

    body = {
        "endpoint": "https://push.example/sub/1",
        "keys": {"p256dh": "abc", "auth": "def"},
    }
    create_res = push_client.post("/v1/push/subscriptions", json=body)
    assert create_res.status_code == 204

    delete_res = push_client.request(
        "DELETE",
        "/v1/push/subscriptions",
        content=json.dumps({"endpoint": body["endpoint"]}),
        headers={"Content-Type": "application/json"},
    )
    assert delete_res.status_code == 204


def test_subscription_upsert_uses_postgres_insert_for_postgres_engine() -> None:
    from sqlalchemy.dialects import postgresql

    class _Dialect:
        name = "postgresql"

    class _Engine:
        dialect = _Dialect()

    stmt = _upsert_insert_for_engine(_Engine())(SqlPushSubscription).values(
        user_id="alice@example.com",
        endpoint="https://push.example/sub/1",
        p256dh="abc",
        auth="def",
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=[SqlPushSubscription.endpoint],
        set_={"user_id": "alice@example.com", "p256dh": "abc", "auth": "def"},
    )

    compiled = str(upsert.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT" in compiled


def test_dispatcher_sends_on_turn_end(db_uri: str) -> None:
    conv_store = SqlAlchemyConversationStore(db_uri)
    sub_store = SqlAlchemyPushSubscriptionStore(db_uri)
    vapid = generate_ephemeral_vapid_keys()
    service = PushNotificationService(
        subscription_store=sub_store,
        permission_store=None,
        conversation_store=conv_store,
        vapid=vapid,
    )
    sub_store.upsert("local", "https://push.example/sub/2", "p256dh", "auth")

    with patch("omnigent.server.push.service.send_web_push", return_value=True) as send_mock:
        service.on_status_change("conv_missing", "running", "idle")
        assert send_mock.call_count == 1
