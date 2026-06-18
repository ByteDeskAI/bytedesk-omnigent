"""Tests for the signed inbound-webhook ingress: HMAC verify + resolve + deliver
end-to-end through the durable signal bus (BDP-2249, ADR-0142)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from omnigent.bus import SqlAlchemySignalBus
from omnigent.ingress import (
    IngressBindingStore,
    IngressStatus,
    process_inbound,
    verify_hmac_signature,
)


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_hmac_signature_bare_and_prefixed() -> None:
    body = b'{"x":1}'
    secret = "s3cr3t"
    assert verify_hmac_signature(body, secret, _sign(body, secret)) is True
    assert verify_hmac_signature(body, secret, "sha256=" + _sign(body, secret)) is True
    assert verify_hmac_signature(body, secret, "deadbeef") is False


def test_process_inbound_delivers_to_signal_bus_then_replay_409(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'ing.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "teamcity-secret"

    # A parked release run awaits the signal; a binding maps the webhook to it.
    bus.register_wait(
        signal_id="release:1.2.3", session_id="sess-rel", key="subscribe:teamcity",
        kind="subscribe", target="teamcity", now=now,
    )
    store.register_binding(
        source="teamcity", match_key="build.finished", signal_id="release:1.2.3", now=now
    )

    body = json.dumps({"build": "green"}).encode()

    # Bad signature -> 401, never reaches the bus.
    bad = process_inbound(
        source="teamcity", raw_body=body, provided_signature="nope", secret=secret,
        store=store, bus=bus, match_key="build.finished", payload={"build": "green"},
    )
    assert bad.status is IngressStatus.BAD_SIGNATURE
    assert bad.http_status == 401

    # Valid signature but no binding for the event -> 404 (never 2xx, BDP-1419).
    nob = process_inbound(
        source="teamcity", raw_body=body, provided_signature=_sign(body, secret),
        secret=secret, store=store, bus=bus, match_key="unknown.event", payload=None,
    )
    assert nob.status is IngressStatus.NO_BINDING
    assert nob.http_status == 404

    # Valid + bound -> delivers to the bus, resolves the parked wait -> 202.
    ok = process_inbound(
        source="teamcity", raw_body=body, provided_signature=_sign(body, secret),
        secret=secret, store=store, bus=bus, match_key="build.finished",
        payload={"build": "green"}, now=now + 1,
    )
    assert ok.status is IngressStatus.DELIVERED
    assert ok.http_status == 202
    assert ok.signal_id == "release:1.2.3"
    assert bus.list_pending(target="teamcity") == []  # the run was woken

    # Replayed delivery (TeamCity retry) -> idempotent 409, no double-fire.
    again = process_inbound(
        source="teamcity", raw_body=body, provided_signature=_sign(body, secret),
        secret=secret, store=store, bus=bus, match_key="build.finished",
        payload=None, now=now + 2,
    )
    assert again.status is IngressStatus.ALREADY_RESOLVED
    assert again.http_status == 409


def test_star_binding_is_per_source_catch_all(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'ing2.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    now = int(time.time())
    secret = "sec"
    bus.register_wait(
        signal_id="sig:any", session_id="s", key="k", kind="subscribe",
        target="gh", now=now,
    )
    store.register_binding(source="github", match_key="*", signal_id="sig:any", now=now)
    body = b"{}"
    res = process_inbound(
        source="github", raw_body=body, provided_signature=_sign(body, secret),
        secret=secret, store=store, bus=bus, match_key="some.event.we.didnt.bind",
        payload=None, now=now + 1,
    )
    assert res.status is IngressStatus.DELIVERED  # fell back to the "*" catch-all
