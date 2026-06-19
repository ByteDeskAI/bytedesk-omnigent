"""Tests for the generic extension seam (ADR-0143, BDP-2291).

Inject fakes so the install logic is proven without installed entry-point metadata
(the entry-point wiring itself is verified live against the rolled gateway).
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.extension import BytedeskExtension
from omnigent.extensions import (
    ENV_VAR,
    OmnigentExtension,
    _load_env_extensions,
    discover_extensions,
    install_extensions,
)


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


def test_load_env_extensions_loads_factory(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "bytedesk_omnigent.extension:BytedeskExtension")
    exts = _load_env_extensions()
    assert [e.name for e in exts] == ["bytedesk"]


def test_load_env_extensions_skips_bad_entry(monkeypatch) -> None:
    monkeypatch.setenv(
        ENV_VAR,
        "nope.module:Missing, bytedesk_omnigent.extension:BytedeskExtension",
    )
    assert [e.name for e in _load_env_extensions()] == ["bytedesk"]


def test_load_env_extensions_empty_is_noop(monkeypatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert _load_env_extensions() == []


def test_discover_dedups_env_against_entrypoint(monkeypatch) -> None:
    # The `bytedesk` entry-point is registered in the synced venv; pointing the env
    # var at the SAME extension must not double-register it.
    monkeypatch.setenv(ENV_VAR, "bytedesk_omnigent.extension:BytedeskExtension")
    names = [e.name for e in discover_extensions()]
    assert names.count("bytedesk") == 1
