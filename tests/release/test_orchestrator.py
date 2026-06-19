"""Tests for the omnigent-native ops-release orchestrator (BDP-2258, ADR-0142).

Proves the park → bind → trigger contract + the duplicate-callback idempotency
that prevents a double-deploy, end-to-end through the real durable signal bus
(BDP-2248) + ingress (BDP-2249), with a fake release executor (the prod-touching
TeamCity trigger is a founder-gated seam, never exercised here).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from omnigent.bus import SqlAlchemySignalBus
from omnigent.ingress import IngressBindingStore, IngressStatus, process_inbound
from omnigent.release import (
    HumanGatedReleaseExecutor,
    ReleaseOrchestrator,
    ReleaseTriggerResult,
    release_signal_id,
)


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _FakeExecutor:
    """Records the trigger call; never touches a real pipeline."""

    def __init__(self, *, on_trigger=None) -> None:
        self.calls: list[dict] = []
        self._on_trigger = on_trigger

    def trigger_release(self, *, version: str, session_id: str) -> ReleaseTriggerResult:
        self.calls.append({"version": version, "session_id": session_id})
        if self._on_trigger is not None:
            self._on_trigger()
        return ReleaseTriggerResult(triggered=True, external_ref=f"tc-{version}")


def _orchestrator(db: str, executor):
    return ReleaseOrchestrator(
        bus=SqlAlchemySignalBus(db),
        binding_store=IngressBindingStore(db),
        executor=executor,
    )


def test_release_signal_id_is_deterministic() -> None:
    assert release_signal_id("1.2.3") == "release:1.2.3"


def test_start_release_parks_binds_then_triggers(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'rel.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    executor = _FakeExecutor()
    orch = ReleaseOrchestrator(bus=bus, binding_store=store, executor=executor)

    result = orch.start_release(version="1.2.3", session_id="sess-rel")

    assert result.signal_id == "release:1.2.3"
    # A durable wait is parked under the release signal.
    pending = bus.list_pending(kind="release")
    assert [w.signal_id for w in pending] == ["release:1.2.3"]
    assert pending[0].session_id == "sess-rel"
    assert pending[0].target == "1.2.3"
    # The TeamCity webhook binding resolves to the same signal.
    binding = store.resolve_binding(source="teamcity", match_key="release:1.2.3")
    assert binding is not None and binding.signal_id == "release:1.2.3"
    # The executor was triggered exactly once with the version + session.
    assert executor.calls == [{"version": "1.2.3", "session_id": "sess-rel"}]
    assert result.trigger.triggered is True


def test_wait_is_parked_before_the_trigger_fires(tmp_path) -> None:
    """Park-before-trigger: a fast callback must always find a pending wait."""
    db = f"sqlite:///{tmp_path / 'rel.db'}"
    bus = SqlAlchemySignalBus(db)
    seen: dict[str, bool] = {}

    def _assert_parked() -> None:
        seen["parked"] = any(
            w.signal_id == "release:9.9.9" for w in bus.list_pending(kind="release")
        )

    executor = _FakeExecutor(on_trigger=_assert_parked)
    orch = ReleaseOrchestrator(
        bus=bus, binding_store=IngressBindingStore(db), executor=executor
    )
    orch.start_release(version="9.9.9", session_id="s")
    assert seen.get("parked") is True


def test_teamcity_callback_resumes_then_duplicate_is_idempotent(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'rel.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    orch = ReleaseOrchestrator(bus=bus, binding_store=store, executor=_FakeExecutor())
    now = int(time.time())
    secret = "teamcity-secret"

    orch.start_release(version="1.2.3", session_id="sess-rel", now=now)

    body = json.dumps({"build": "green"}).encode()
    first = process_inbound(
        source="teamcity", raw_body=body, provided_signature=_sign(body, secret),
        secret=secret, store=store, bus=bus, match_key="release:1.2.3",
        payload={"build": "green"}, now=now,
    )
    assert first.status is IngressStatus.DELIVERED
    assert first.http_status == 202
    # The parked release session is woken with the callback payload.
    inbox = bus.drain_inbox(session_id="sess-rel")
    assert [m["payload"] for m in inbox] == [{"build": "green"}]

    # A REPLAYED TeamCity callback must NOT wake again (no double-deploy).
    dup = process_inbound(
        source="teamcity", raw_body=body, provided_signature=_sign(body, secret),
        secret=secret, store=store, bus=bus, match_key="release:1.2.3",
        payload={"build": "green"}, now=now,
    )
    assert dup.status is IngressStatus.ALREADY_RESOLVED
    assert dup.http_status == 409
    assert bus.drain_inbox(session_id="sess-rel") == []


def test_start_release_is_idempotent_on_retry(tmp_path) -> None:
    """A retried start re-parks the same wait + binding (no duplicate park)."""
    db = f"sqlite:///{tmp_path / 'rel.db'}"
    bus = SqlAlchemySignalBus(db)
    store = IngressBindingStore(db)
    orch = ReleaseOrchestrator(bus=bus, binding_store=store, executor=_FakeExecutor())

    orch.start_release(version="2.0.0", session_id="s")
    orch.start_release(version="2.0.0", session_id="s")

    assert len(bus.list_pending(kind="release")) == 1


def test_human_gated_executor_refuses_to_trigger() -> None:
    """The default executor is a founder-gated placeholder — it must NOT deploy."""
    with pytest.raises(NotImplementedError):
        HumanGatedReleaseExecutor().trigger_release(version="1.2.3", session_id="s")
