"""Route tests for signed inbound-webhook ingress (BDP-2249)."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.ingress import IngressResult, IngressStatus
from bytedesk_omnigent.routes.ingress import create_ingress_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_ingress_router(), prefix="/v1")
    return TestClient(app)


def test_ingress_route_unknown_source_returns_404(monkeypatch) -> None:
    monkeypatch.setattr(
        "bytedesk_omnigent.ingress.resolve_secret",
        lambda _source: None,
    )

    response = _client().post("/v1/ingress/teamcity", content=b"{}")
    assert response.status_code == 404
    assert response.json()["status"] == "unknown_source"


def test_ingress_route_delegates_to_process_inbound(monkeypatch) -> None:
    monkeypatch.setattr("bytedesk_omnigent.ingress.resolve_secret", lambda _s: "secret")
    monkeypatch.setattr(
        "bytedesk_omnigent.ingress.resolve_webhook_adapter",
        lambda _s: object(),
    )
    monkeypatch.setattr("bytedesk_omnigent.ingress.get_binding_store", lambda: object())
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_signal_bus", lambda: object())

    def _process(**_kwargs: object) -> IngressResult:
        return IngressResult(
            IngressStatus.DELIVERED,
            200,
            signal_id="sig_1",
            detail="ok",
        )

    monkeypatch.setattr("bytedesk_omnigent.ingress.process_inbound", _process)

    response = _client().post(
        "/v1/ingress/github",
        content=json.dumps({"event": "push"}).encode(),
        headers={"X-Omnigent-Event": "push"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "delivered",
        "signal_id": "sig_1",
        "detail": "ok",
    }


def test_ingress_route_treats_invalid_json_as_none_payload(monkeypatch) -> None:
    monkeypatch.setattr("bytedesk_omnigent.ingress.resolve_secret", lambda _s: "secret")
    monkeypatch.setattr(
        "bytedesk_omnigent.ingress.resolve_webhook_adapter",
        lambda _s: object(),
    )
    monkeypatch.setattr("bytedesk_omnigent.ingress.get_binding_store", lambda: object())
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_signal_bus", lambda: object())

    seen: dict[str, object | None] = {}

    def _process(**kwargs: object) -> IngressResult:
        seen.update(kwargs)
        return IngressResult(IngressStatus.BAD_SIGNATURE, 401, detail="bad sig")

    monkeypatch.setattr("bytedesk_omnigent.ingress.process_inbound", _process)

    response = _client().post("/v1/ingress/github", content=b"not-json")
    assert response.status_code == 401
    assert seen["payload"] is None
