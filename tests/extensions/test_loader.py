"""Tests for the generic extension seam (ADR-0143, BDP-2291).

Inject fakes so the install logic is proven without installed entry-point metadata
(the entry-point wiring itself is verified live against the rolled gateway).
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.extension import BytedeskExtension
from omnigent.extensions import OmnigentExtension, install_extensions


class _FakeExt:
    name = "fake"

    def __init__(self) -> None:
        self._router = APIRouter()

        @self._router.get("/_fake/ping")
        async def ping() -> dict:
            return {"ok": True}

    def routers(self) -> list[APIRouter]:
        return [self._router]


def test_install_extensions_mounts_routers_under_v1() -> None:
    app = FastAPI()
    installed = install_extensions(app, extensions=[_FakeExt()])
    assert installed == ["fake"]
    client = TestClient(app)
    resp = client.get("/v1/_fake/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_install_extensions_with_none_is_noop() -> None:
    assert install_extensions(FastAPI(), extensions=[]) == []


def test_bytedesk_extension_satisfies_protocol_and_serves_health() -> None:
    ext = BytedeskExtension()
    assert isinstance(ext, OmnigentExtension)  # structural (runtime_checkable)
    app = FastAPI()
    assert install_extensions(app, extensions=[ext]) == ["bytedesk"]
    resp = TestClient(app).get("/v1/_ext/health")
    assert resp.status_code == 200
    assert resp.json() == {"extension": "bytedesk", "loaded": True}
