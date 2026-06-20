"""Seam tests for the suppression-store Protocol (BDP-2349 #46).

Proves: the concrete store satisfies the :class:`SuppressionStore` Protocol, a
fake backend can be injected via set_suppression_store, the override wins over
the SqlAlchemy default, and outreach_compliance enforces against the injected
backend.
"""
from __future__ import annotations

import pytest

from bytedesk_omnigent import compliance
from bytedesk_omnigent.compliance import (
    SqlAlchemySuppressionStore,
    SuppressionStore,
    get_suppression_store,
    set_suppression_store,
)
from bytedesk_omnigent.policies.outreach_compliance import outreach_compliance


@pytest.fixture(autouse=True)
def _restore_store():
    yield
    set_suppression_store(None)


class _FakeSuppressionStore:
    def __init__(self, suppressed: set[tuple[str, str]] | None = None) -> None:
        self._suppressed = suppressed or set()

    def suppress(
        self, *, channel: str, address: str, reason: str, now: int | None = None
    ) -> bool:
        before = len(self._suppressed)
        self._suppressed.add((channel, address.strip().lower()))
        return len(self._suppressed) > before

    def is_suppressed(self, *, channel: str, address: str) -> bool:
        return (channel, address.strip().lower()) in self._suppressed

    def list_suppressed(self, *, channel: str | None = None):
        return []


def test_sqlalchemy_store_satisfies_protocol(tmp_path) -> None:
    store = SqlAlchemySuppressionStore(f"sqlite:///{tmp_path / 'sup.db'}")
    assert isinstance(store, SuppressionStore)


def test_fake_backend_satisfies_protocol() -> None:
    assert isinstance(_FakeSuppressionStore(), SuppressionStore)


def test_injected_backend_wins_over_default() -> None:
    fake = _FakeSuppressionStore({("email", "blocked@x.com")})
    set_suppression_store(fake)
    # No DB / conversation store touched — the override short-circuits.
    assert get_suppression_store() is fake
    assert get_suppression_store().is_suppressed(channel="email", address="BLOCKED@x.com")


def test_outreach_policy_enforces_against_injected_backend() -> None:
    set_suppression_store(_FakeSuppressionStore({("email", "blocked@x.com")}))
    policy = outreach_compliance(["email\\.send"])  # is_suppressed self-resolves
    event = {
        "type": "tool_call",
        "data": {
            "name": "email.send",
            "arguments": {"to": "blocked@x.com", "unsubscribe_url": "https://u"},
        },
    }
    assert policy(event)["result"] == "DENY"

    ok_event = {
        "type": "tool_call",
        "data": {
            "name": "email.send",
            "arguments": {"to": "fine@x.com", "unsubscribe_url": "https://u"},
        },
    }
    assert policy(ok_event)["result"] == "ALLOW"
