"""Push notification dispatcher."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from omnigent.server.auth import RESERVED_USER_PUBLIC
from omnigent.server.push.attention import should_notify_new_elicitation, should_notify_turn_end
from omnigent.server.push.sender import build_push_payload, send_web_push
from omnigent.server.push.vapid import VapidKeys, load_vapid_keys
from omnigent.stores.push_subscription_store import PushSubscriptionStore

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

_service: "PushNotificationService | None" = None

TURN_END_BODY = "Agent finished and is ready for your input."
ELICITATION_BODY = "Agent is asking for your input."


class PushNotificationService:
    """Dispatch Web Push for session attention events."""

    def __init__(
        self,
        *,
        subscription_store: PushSubscriptionStore,
        permission_store: PermissionStore | None,
        conversation_store: ConversationStore,
        vapid: VapidKeys | None,
    ) -> None:
        self._subscription_store = subscription_store
        self._permission_store = permission_store
        self._conversation_store = conversation_store
        self._vapid = vapid
        self._elicitation_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def vapid_public_key(self) -> str | None:
        return self._vapid.public_key if self._vapid else None

    def on_status_change(self, session_id: str, previous_status: str | None, new_status: str) -> None:
        if not self._vapid:
            return
        if not should_notify_turn_end(previous_status, new_status):
            return
        title = self._session_title(session_id)
        self._dispatch(
            session_id,
            title=title,
            body=TURN_END_BODY,
            kind="turn_end",
        )

    def on_elicitation_count_change(
        self,
        session_id: str,
        previous_count: int | None,
        new_count: int,
    ) -> None:
        if not self._vapid:
            return
        if not should_notify_new_elicitation(previous_count, new_count):
            return
        title = self._session_title(session_id)
        self._dispatch(
            session_id,
            title=title,
            body=ELICITATION_BODY,
            kind="elicitation",
        )

    def record_elicitation_count(self, session_id: str, count: int) -> None:
        """Track counts for transition detection (WS updates path)."""
        with self._lock:
            previous = self._elicitation_counts.get(session_id)
            self._elicitation_counts[session_id] = count
        self.on_elicitation_count_change(session_id, previous, count)

    def on_new_elicitation(self, session_id: str) -> None:
        """Hook from pending_elicitations when a request is published."""
        with self._lock:
            previous = self._elicitation_counts.get(session_id, 0)
            new_count = previous + 1
            self._elicitation_counts[session_id] = new_count
        self.on_elicitation_count_change(session_id, previous, new_count)

    def _session_title(self, session_id: str) -> str:
        conv = self._conversation_store.get_conversation(session_id)
        if conv is None:
            return "Omnigent"
        return conv.title or conv.id

    def _recipient_user_ids(self, session_id: str) -> list[str]:
        if self._permission_store is None:
            return ["local"]
        grants = self._permission_store.list_for_session(session_id)
        user_ids = {grant.user_id for grant in grants if grant.user_id != RESERVED_USER_PUBLIC}
        if not user_ids:
            return ["local"]
        return sorted(user_ids)

    def _dispatch(self, session_id: str, *, title: str, body: str, kind: str) -> None:
        user_ids = self._recipient_user_ids(session_id)
        subscriptions = self._subscription_store.list_for_users(user_ids)
        if not subscriptions or self._vapid is None:
            return
        payload = build_push_payload(session_id=session_id, title=title, body=body, kind=kind)
        for sub in subscriptions:
            send_web_push(sub, payload, self._vapid)


def set_push_service(service: PushNotificationService | None) -> None:
    global _service
    _service = service


def get_push_service() -> PushNotificationService | None:
    return _service


def create_push_service(
    *,
    subscription_store: PushSubscriptionStore,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> PushNotificationService | None:
    vapid = load_vapid_keys()
    if vapid is None:
        _logger.info("Web Push disabled: no VAPID keys configured")
        return None
    return PushNotificationService(
        subscription_store=subscription_store,
        permission_store=permission_store,
        conversation_store=conversation_store,
        vapid=vapid,
    )