"""Web Push subscription routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from omnigent.server.push.service import get_push_service
from omnigent.stores.push_subscription_store import PushSubscriptionStore


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionCreate(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys


class PushSubscriptionDelete(BaseModel):
    endpoint: str


class VapidPublicKeyResponse(BaseModel):
    public_key: str


def create_push_router(
    subscription_store: PushSubscriptionStore,
    auth_provider: object | None,
) -> APIRouter:
    router = APIRouter()

    def _user_id(request: Request) -> str:
        if auth_provider is None:
            return "local"
        user_id = auth_provider.get_user_id(request)  # type: ignore[attr-defined]
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return str(user_id)

    @router.get("/push/vapid-public-key", response_model=VapidPublicKeyResponse)
    def vapid_public_key() -> VapidPublicKeyResponse:
        service = get_push_service()
        if service is None or service.vapid_public_key is None:
            raise HTTPException(status_code=503, detail="Web Push not configured")
        return VapidPublicKeyResponse(public_key=service.vapid_public_key)

    @router.post("/push/subscriptions", status_code=204)
    def create_subscription(request: Request, body: PushSubscriptionCreate) -> None:
        user_id = _user_id(request)
        subscription_store.upsert(
            user_id=user_id,
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
        )

    @router.delete("/push/subscriptions", status_code=204)
    def delete_subscription(request: Request, body: PushSubscriptionDelete) -> None:
        user_id = _user_id(request)
        subscription_store.delete(user_id=user_id, endpoint=body.endpoint)

    return router