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


# ── ADR-0155 cutover flag (BDP-2566): default OFF = no behavior change ──────────


class _VerifyingAdapter:
    """Adapter that verifies + resolves a match key (pipeline SignalTranslator uses it)."""

    def __init__(self, *, ok: bool = True) -> None:
        self._ok = ok

    def verify(self, _raw: bytes, _headers, _secret: str) -> bool:
        return self._ok

    def match_key(self, _headers) -> str:
        return "*"


async def _async_true(*_args, **_kwargs) -> bool:
    return True


def _boom_ingest(**_kwargs):  # pragma: no cover - only invoked on regression
    raise AssertionError("ingest() must not run while the cutover flag is OFF")


def test_flag_off_by_default_uses_legacy_process_inbound(monkeypatch) -> None:
    """Default flag state (unset → off) keeps the legacy process_inbound path."""
    monkeypatch.setattr("bytedesk_omnigent.ingress.resolve_secret", lambda _s: "secret")
    monkeypatch.setattr(
        "bytedesk_omnigent.ingress.resolve_webhook_adapter", lambda _s: _VerifyingAdapter()
    )
    monkeypatch.setattr("bytedesk_omnigent.ingress.get_binding_store", lambda: object())
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_signal_bus", lambda: object())
    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _boom_ingest)

    called = {"n": 0}

    def _process(**_kwargs) -> IngressResult:
        called["n"] += 1
        return IngressResult(IngressStatus.DELIVERED, 202, signal_id="sig_1")

    monkeypatch.setattr("bytedesk_omnigent.ingress.process_inbound", _process)

    response = _client().post("/v1/ingress/github", content=json.dumps({"event": "push"}).encode())
    assert response.status_code == 202
    assert called["n"] == 1


def test_flag_on_routes_json_body_through_pipeline(monkeypatch) -> None:
    """Flag ON + JSON object body → verified, then handed to ``ingest`` on the signal channel."""
    from bytedesk_omnigent.inbound.pipeline import IngestResult

    monkeypatch.setattr("bytedesk_omnigent.ingress.resolve_secret", lambda _s: "secret")
    monkeypatch.setattr(
        "bytedesk_omnigent.ingress.resolve_webhook_adapter", lambda _s: _VerifyingAdapter()
    )
    monkeypatch.setattr("bytedesk_omnigent.inbound.flags.evaluate_inbound_flag", _async_true)

    calls: dict = {}

    def _fake_ingest(**kwargs):
        calls.update(kwargs)
        return IngestResult(
            status="projected", http_status=202, idempotency_key="k-1",
            event_type="signal.deliver",
        )

    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _fake_ingest)
    monkeypatch.setattr(
        "bytedesk_omnigent.inbound.store.get_inbound_event_store", lambda: object()
    )
    monkeypatch.setattr("bytedesk_omnigent.inbound.processors.all_processors", list)

    response = _client().post("/v1/ingress/github", content=json.dumps({"event": "push"}).encode())

    assert response.status_code == 202
    assert response.json()["status"] == "projected"
    assert response.json()["idempotencyKey"] == "k-1"
    assert calls["channel"] == "signal"
    assert calls["source"] == "github"
    assert calls["raw_payload"] == {"event": "push"}


def test_flag_on_bad_signature_returns_401_without_pipeline(monkeypatch) -> None:
    """Signature verification stays the auth boundary before the pipeline runs."""
    monkeypatch.setattr("bytedesk_omnigent.ingress.resolve_secret", lambda _s: "secret")
    monkeypatch.setattr(
        "bytedesk_omnigent.ingress.resolve_webhook_adapter",
        lambda _s: _VerifyingAdapter(ok=False),
    )
    monkeypatch.setattr("bytedesk_omnigent.inbound.flags.evaluate_inbound_flag", _async_true)
    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _boom_ingest)

    response = _client().post("/v1/ingress/github", content=json.dumps({"event": "push"}).encode())
    assert response.status_code == 401
    assert response.json()["status"] == "bad_signature"


def test_flag_on_non_json_body_falls_through_to_legacy(monkeypatch) -> None:
    """A non-JSON body can't be an ``ingest`` payload → legacy passthrough (payload=None)."""
    monkeypatch.setattr("bytedesk_omnigent.ingress.resolve_secret", lambda _s: "secret")
    monkeypatch.setattr(
        "bytedesk_omnigent.ingress.resolve_webhook_adapter", lambda _s: _VerifyingAdapter()
    )
    monkeypatch.setattr("bytedesk_omnigent.ingress.get_binding_store", lambda: object())
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_signal_bus", lambda: object())
    # Flag is ON, but the non-dict body must never reach the pipeline.
    monkeypatch.setattr("bytedesk_omnigent.inbound.flags.evaluate_inbound_flag", _async_true)
    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _boom_ingest)

    seen: dict[str, object | None] = {}

    def _process(**kwargs) -> IngressResult:
        seen.update(kwargs)
        return IngressResult(IngressStatus.BAD_SIGNATURE, 401, detail="bad sig")

    monkeypatch.setattr("bytedesk_omnigent.ingress.process_inbound", _process)

    response = _client().post("/v1/ingress/github", content=b"not-json")
    assert response.status_code == 401
    assert seen["payload"] is None
