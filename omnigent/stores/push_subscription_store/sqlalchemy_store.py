"""SQLAlchemy-backed push subscription store."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from omnigent.db.db_models import SqlPushSubscription
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.stores.push_subscription_store import PushSubscription, PushSubscriptionStore


class SqlAlchemyPushSubscriptionStore(PushSubscriptionStore):
    """Persist push subscriptions in the omnigent database."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def upsert(self, user_id: str, endpoint: str, p256dh: str, auth: str) -> PushSubscription:
        with self._session() as session:
            stmt = sqlite_insert(SqlPushSubscription).values(
                user_id=user_id,
                endpoint=endpoint,
                p256dh=p256dh,
                auth=auth,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SqlPushSubscription.endpoint],
                set_={
                    "user_id": user_id,
                    "p256dh": p256dh,
                    "auth": auth,
                },
            )
            session.execute(stmt)
            session.commit()
        return PushSubscription(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth)

    def delete(self, user_id: str, endpoint: str) -> None:
        with self._session() as session:
            session.execute(
                delete(SqlPushSubscription).where(
                    SqlPushSubscription.user_id == user_id,
                    SqlPushSubscription.endpoint == endpoint,
                )
            )
            session.commit()

    def delete_all_for_user(self, user_id: str) -> None:
        with self._session() as session:
            session.execute(delete(SqlPushSubscription).where(SqlPushSubscription.user_id == user_id))
            session.commit()

    def list_for_user(self, user_id: str) -> list[PushSubscription]:
        with self._session() as session:
            rows = session.scalars(
                select(SqlPushSubscription).where(SqlPushSubscription.user_id == user_id)
            ).all()
            return [
                PushSubscription(
                    user_id=row.user_id,
                    endpoint=row.endpoint,
                    p256dh=row.p256dh,
                    auth=row.auth,
                )
                for row in rows
            ]

    def list_for_users(self, user_ids: list[str]) -> list[PushSubscription]:
        if not user_ids:
            return []
        with self._session() as session:
            rows = session.scalars(
                select(SqlPushSubscription).where(SqlPushSubscription.user_id.in_(user_ids))
            ).all()
            return [
                PushSubscription(
                    user_id=row.user_id,
                    endpoint=row.endpoint,
                    p256dh=row.p256dh,
                    auth=row.auth,
                )
                for row in rows
            ]