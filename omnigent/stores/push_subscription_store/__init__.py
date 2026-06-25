"""Push subscription persistence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PushSubscription:
    """A browser push subscription for one user/device."""

    user_id: str
    endpoint: str
    p256dh: str
    auth: str


class PushSubscriptionStore:
    """Abstract store for Web Push subscriptions."""

    def upsert(self, user_id: str, endpoint: str, p256dh: str, auth: str) -> PushSubscription:
        """Create or update a subscription for *user_id*."""
        raise NotImplementedError

    def delete(self, user_id: str, endpoint: str) -> None:
        """Remove a subscription."""
        raise NotImplementedError

    def delete_all_for_user(self, user_id: str) -> None:
        """Remove every subscription for *user_id* (logout)."""
        raise NotImplementedError

    def list_for_user(self, user_id: str) -> list[PushSubscription]:
        """Return all subscriptions for *user_id*."""
        raise NotImplementedError

    def list_for_users(self, user_ids: list[str]) -> list[PushSubscription]:
        """Return subscriptions for multiple users."""
        raise NotImplementedError