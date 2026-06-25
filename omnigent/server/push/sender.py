"""Web Push delivery."""

from __future__ import annotations

import json
import logging
from typing import Any

from omnigent.server.push.vapid import VapidKeys
from omnigent.stores.push_subscription_store import PushSubscription

_logger = logging.getLogger(__name__)


def build_push_payload(
    *,
    session_id: str,
    title: str,
    body: str,
    kind: str,
) -> dict[str, Any]:
    """JSON envelope consumed by ap-web service worker."""
    return {
        "sessionId": session_id,
        "session_id": session_id,
        "kind": kind,
        "title": title,
        "body": body,
        "url": f"/c/{session_id}",
    }


def send_web_push(
    subscription: PushSubscription,
    payload: dict[str, Any],
    vapid: VapidKeys,
) -> bool:
    """
    Send one Web Push notification.

    Returns True when the provider accepted the message.
    """
    try:
        from pywebpush import webpush
    except ImportError:
        _logger.warning("pywebpush not installed; skipping push delivery")
        return False

    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
    }
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=vapid.private_key,
            vapid_claims={"sub": f"mailto:{vapid.claims_email}"},
        )
        return True
    except Exception:
        _logger.exception("web push delivery failed for %s", subscription.endpoint)
        return False