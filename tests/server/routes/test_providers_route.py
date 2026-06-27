"""Tests for the provider register/list + canonical ingress route (Phase 4, BDP-2586)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.engine.providers.registry import ProviderRegistry
from bytedesk_omnigent.routes.providers import create_providers_router
from omnigent.errors import OmnigentError


class _AuthWithUser:
    def get_user_id(self, request: object) -> str:
        return "u-1"


class _NonAdminStore:
    def is_admin(self, user_id: str) -> bool:
        return False


def _app(auth_provider=None, permission_store=None) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(status_code=exc.http_status, content={"error": exc.code}),
    )
    app.include_router(
        create_providers_router(auth_provider=auth_provider, permission_store=permission_store),
        prefix="/v1",
    )
    return app


def _fresh_registry(monkeypatch) -> ProviderRegistry:
    reg = ProviderRegistry()
    monkeypatch.setattr(
        "bytedesk_omnigent.engine.providers.registry.get_provider_registry", lambda: reg
    )
    return reg


_MANIFEST = {
    "name": "bytedesk",
    "base_url": "https://platform.bytedesk.ai/api/engine",
    "sensors": ["jira_issue"],
    "actuators": [{"name": "send_email", "risk_tier": 3}],
    "outcomes": ["outcome.booked"],
    "webhook_sources": ["stripe"],
    "auth": {"header": "X-Engine-Secret", "secret": "sssh"},
}


def test_register_and_list_provider_single_user(monkeypatch) -> None:
    reg = _fresh_registry(monkeypatch)
    client = TestClient(_app())  # single-user mode → open

    r = client.post("/v1/goal-providers/register", json=_MANIFEST)
    assert r.status_code == 201
    assert r.json()["provider"]["name"] == "bytedesk"
    assert "secret" not in str(r.json())  # secret never echoed

    listed = client.get("/v1/goal-providers")
    assert listed.status_code == 200
    assert [p["name"] for p in listed.json()["providers"]] == ["bytedesk"]
    assert reg.get("bytedesk") is not None


def test_register_requires_admin(monkeypatch) -> None:
    _fresh_registry(monkeypatch)
    client = TestClient(
        _app(auth_provider=_AuthWithUser(), permission_store=_NonAdminStore()),
        raise_server_exceptions=False,
    )
    r = client.post("/v1/goal-providers/register", json=_MANIFEST)
    assert r.status_code == 403


def test_canonical_ingress_gated_off_by_default(monkeypatch) -> None:
    _fresh_registry(monkeypatch)
    # default-off flag → 202 disabled, pipeline not run (no store touched)
    monkeypatch.setattr(
        "bytedesk_omnigent.inbound.flags.evaluate_inbound_flag",
        _async_false,
    )
    client = TestClient(_app())
    r = client.post("/v1/inbound/events", json={"type": "custom.signal"})
    assert r.status_code == 202
    assert r.json()["status"] == "disabled"


def test_canonical_ingress_runs_pipeline_when_enabled(monkeypatch) -> None:
    _fresh_registry(monkeypatch)
    monkeypatch.setattr(
        "bytedesk_omnigent.inbound.flags.evaluate_inbound_flag", _async_true
    )

    calls: dict = {}

    def _fake_ingest(**kwargs):
        from bytedesk_omnigent.inbound.pipeline import IngestResult

        calls.update(kwargs)
        return IngestResult(status="projected", http_status=202, idempotency_key="k-1",
                            event_type="custom.signal")

    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _fake_ingest)
    monkeypatch.setattr(
        "bytedesk_omnigent.inbound.store.get_inbound_event_store", lambda: object()
    )
    monkeypatch.setattr(
        "bytedesk_omnigent.inbound.processors.all_processors", lambda: []
    )

    client = TestClient(_app())
    r = client.post("/v1/inbound/events", json={"type": "custom.signal", "source": "bytedesk"})
    assert r.status_code == 202
    assert r.json()["status"] == "projected"
    assert calls["channel"] == "provider"
    assert calls["source"] == "bytedesk"


async def _async_true(*args, **kwargs) -> bool:
    return True


async def _async_false(*args, **kwargs) -> bool:
    return False
