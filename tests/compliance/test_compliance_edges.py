"""Edge tests for suppression store helpers and lazy resolution."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy.exc import IntegrityError

from bytedesk_omnigent.compliance import (
    SqlAlchemySuppressionStore,
    Suppression,
    _to_suppression,
    get_suppression_store,
    set_suppression_store,
)
from bytedesk_omnigent.db_models import SqlSuppression


@pytest.fixture(autouse=True)
def _restore_store() -> None:
    yield
    set_suppression_store(None)
    get_suppression_store.__globals__["_suppression_store_cache"].clear()


def test_to_suppression_maps_row_fields() -> None:
    row = SqlSuppression(
        channel="email",
        address="a@x.com",
        reason="unsubscribe",
        created_at=42,
    )
    sup = _to_suppression(row)
    assert sup == Suppression(
        channel="email", address="a@x.com", reason="unsubscribe", created_at=42
    )


def test_list_suppressed_filters_by_channel_and_orders(tmp_path) -> None:
    store = SqlAlchemySuppressionStore(f"sqlite:///{tmp_path / 'sup.db'}")
    assert store.engine is not None
    store.suppress(channel="email", address="a@x.com", reason="unsubscribe", now=10)
    store.suppress(channel="sms", address="+1555", reason="complaint", now=20)
    store.suppress(channel="email", address="b@x.com", reason="gdpr_erasure", now=30)

    all_rows = store.list_suppressed()
    assert [r.address for r in all_rows] == ["b@x.com", "+1555", "a@x.com"]

    email_only = store.list_suppressed(channel="email")
    assert [r.address for r in email_only] == ["b@x.com", "a@x.com"]


def test_suppress_handles_integrity_error_race(tmp_path, monkeypatch) -> None:
    store = SqlAlchemySuppressionStore(f"sqlite:///{tmp_path / 'sup.db'}")

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> None:
            return None

        def add(self, *_args: object, **_kwargs: object) -> None:
            return None

        def flush(self) -> None:
            raise IntegrityError("insert", {}, Exception("dup"))

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(store, "_write_session", lambda: _Session())
    assert store.suppress(channel="email", address="a@x.com", reason="unsubscribe") is False


@dataclass
class _FakeConversationStore:
    storage_location: str


def test_get_suppression_store_caches_by_location(monkeypatch, tmp_path) -> None:
    location = f"sqlite:///{tmp_path / 'conv.db'}"

    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store",
        lambda: _FakeConversationStore(storage_location=location),
    )

    first = get_suppression_store()
    second = get_suppression_store()
    assert first is second
    assert isinstance(first, SqlAlchemySuppressionStore)
