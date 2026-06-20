"""Seam tests for the idempotency store Protocol (BDP-2349 #12).

Proves: the concrete store satisfies the :class:`IdempotencyStore` Protocol,
a fake impl can be substituted, and the SqlAlchemy default preserves the
IntegrityError at-most-once contract (the first claimer wins, a duplicate loses).
"""
from __future__ import annotations

from bytedesk_omnigent.idempotency import (
    IdempotencyStore,
    SqlAlchemyIdempotencyStore,
)


def test_sqlalchemy_store_satisfies_protocol(tmp_path) -> None:
    store = SqlAlchemyIdempotencyStore(f"sqlite:///{tmp_path / 'idem.db'}")
    assert isinstance(store, IdempotencyStore)


def test_fake_impl_satisfies_protocol_and_swaps() -> None:
    class FakeIdempotencyStore:
        def __init__(self) -> None:
            self._claimed: set[tuple[str, str]] = set()
            self._dead: set[tuple[str, str]] = set()

        def claim(self, *, scope: str, key: str, now: int | None = None) -> bool:
            if (scope, key) in self._claimed:
                return False
            self._claimed.add((scope, key))
            return True

        def is_claimed(self, *, scope: str, key: str) -> bool:
            return (scope, key) in self._claimed

        def mark_dead_lettered(
            self, *, scope: str, key: str, result: dict | None = None
        ) -> None:
            self._dead.add((scope, key))

    fake: IdempotencyStore = FakeIdempotencyStore()
    assert isinstance(fake, IdempotencyStore)
    assert fake.claim(scope="s", key="k") is True
    assert fake.claim(scope="s", key="k") is False
    assert fake.is_claimed(scope="s", key="k") is True


def test_default_preserves_at_most_once_integrity_contract(tmp_path) -> None:
    """The SqlAlchemy default's atomic guard: first caller wins, duplicate loses."""
    store: IdempotencyStore = SqlAlchemyIdempotencyStore(
        f"sqlite:///{tmp_path / 'idem.db'}"
    )
    assert store.claim(scope="event", key="dup") is True
    # The composite-PK IntegrityError is swallowed into a False — at most once.
    assert store.claim(scope="event", key="dup") is False
    assert store.claim(scope="event", key="dup") is False
    assert store.is_claimed(scope="event", key="dup") is True
