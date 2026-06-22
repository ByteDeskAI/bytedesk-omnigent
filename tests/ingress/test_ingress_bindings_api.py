"""Tests for webhook binding management API (iteration 13)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.ingress import IngressBindingStore
from bytedesk_omnigent.routes.ingress import create_ingress_router


def test_store_lists_bindings_deterministically(tmp_path) -> None:
    store = IngressBindingStore(f"sqlite:///{tmp_path / 'bindings.db'}")

    store.register_binding(source="slack", match_key="message", signal_id="sig:slack")
    store.register_binding(source="github", match_key="pull_request", signal_id="sig:gh")
    store.register_binding(source="github", match_key="issues", signal_id="sig:issues")

    assert [(b.source, b.match_key, b.signal_id) for b in store.list_bindings()] == [
        ("github", "issues", "sig:issues"),
        ("github", "pull_request", "sig:gh"),
        ("slack", "message", "sig:slack"),
    ]
    assert [(b.match_key, b.signal_id) for b in store.list_bindings(source="github")] == [
        ("issues", "sig:issues"),
        ("pull_request", "sig:gh"),
    ]


def test_ingress_binding_api_registers_and_lists(tmp_path, monkeypatch) -> None:
    store = IngressBindingStore(f"sqlite:///{tmp_path / 'bindings_api.db'}")
    monkeypatch.setattr("bytedesk_omnigent.ingress.get_binding_store", lambda: store)

    app = FastAPI()
    app.include_router(create_ingress_router(), prefix="/v1")
    client = TestClient(app)

    created = client.post(
        "/v1/ingress-bindings",
        json={
            "source": "github",
            "match_key": "pull_request",
            "signal_id": "workflow:review-ready",
        },
    )
    assert created.status_code == 201
    assert created.json()["binding"] == {
        "id": created.json()["binding"]["id"],
        "source": "github",
        "match_key": "pull_request",
        "signal_id": "workflow:review-ready",
        "enabled": True,
    }

    listed = client.get("/v1/ingress-bindings", params={"source": "github"})
    assert listed.status_code == 200
    assert listed.json()["bindings"] == [created.json()["binding"]]

    updated = client.post(
        "/v1/ingress-bindings",
        json={
            "source": "github",
            "match_key": "pull_request",
            "signal_id": "workflow:review-rerun",
        },
    )
    assert updated.status_code == 201
    assert updated.json()["binding"]["id"] == created.json()["binding"]["id"]
    assert updated.json()["binding"]["signal_id"] == "workflow:review-rerun"
    assert client.get("/v1/ingress-bindings").json()["bindings"] == [
        updated.json()["binding"]
    ]


def test_ingress_binding_api_rejects_missing_required_fields(tmp_path, monkeypatch) -> None:
    store = IngressBindingStore(f"sqlite:///{tmp_path / 'bindings_bad.db'}")
    monkeypatch.setattr("bytedesk_omnigent.ingress.get_binding_store", lambda: store)

    app = FastAPI()
    app.include_router(create_ingress_router(), prefix="/v1")
    client = TestClient(app)

    response = client.post("/v1/ingress-bindings", json={"source": "github"})

    assert response.status_code == 400
    assert response.json()["status"] == "invalid_request"
    assert store.list_bindings() == []
