"""Edge tests for ingress header lookup, dead-letter, and store cache."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import bytedesk_omnigent.ingress as ingress_mod
from bytedesk_omnigent.bus import SqlAlchemySignalBus
from bytedesk_omnigent.ingress import (
    GitHubWebhookAdapter,
    IngressBindingStore,
    IngressStatus,
    _header,
    get_binding_store,
    process_inbound,
    register_webhook_adapter,
    resolve_webhook_adapter,
)


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _hdrs(signature: str, event: str) -> dict[str, str]:
    return {"x-omnigent-signature": signature, "x-omnigent-event": event}


def test_header_lookup_is_case_insensitive() -> None:
    assert _header({"X-Omnigent-Event": "push"}, "x-omnigent-event") == "push"
    assert GitHubWebhookAdapter().match_key({"X-Omnigent-Event": "deploy"}) == "deploy"


def test_process_inbound_dead_letters_when_no_pending_wait(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'ing-dead.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "sec"
    store.register_binding(source="gh", match_key="push", signal_id="sig:missing", now=now)
    body = b"{}"
    res = process_inbound(
        source="gh",
        raw_body=body,
        headers=_hdrs(_sign(body, secret), "push"),
        secret=secret,
        store=store,
        bus=bus,
        payload=json.loads(body.decode()),
        now=now + 1,
    )
    assert res.status is IngressStatus.DEAD_LETTERED
    assert res.http_status == 404


def test_binding_store_exposes_engine(tmp_path) -> None:
    store = IngressBindingStore(f"sqlite:///{tmp_path / 'bind.db'}")
    assert store.engine is not None


@dataclass
class _FakeConversationStore:
    storage_location: str


def test_register_webhook_adapter_builds_registry_when_uninitialized() -> None:
    saved = ingress_mod._webhook_adapter_registry
    try:
        ingress_mod._webhook_adapter_registry = None

        class _CustomAdapter:
            def verify(self, raw_body, headers, secret) -> bool:
                return True

            def match_key(self, headers) -> str:
                return headers.get("x-event", "*")

        register_webhook_adapter("custom", _CustomAdapter)
        adapter = resolve_webhook_adapter("custom")
        assert isinstance(adapter, _CustomAdapter)
    finally:
        ingress_mod._webhook_adapter_registry = saved


def test_get_binding_store_caches_by_location(monkeypatch, tmp_path) -> None:
    location = f"sqlite:///{tmp_path / 'conv.db'}"
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore(storage_location=location),
    )
    get_binding_store.__globals__["_binding_store_cache"].clear()

    first = get_binding_store()
    second = get_binding_store()
    assert first is second
    assert isinstance(first, IngressBindingStore)
