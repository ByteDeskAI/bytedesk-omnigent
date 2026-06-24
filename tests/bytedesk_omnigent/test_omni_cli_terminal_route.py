from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bytedesk_omnigent.routes.omni_cli_terminal import (
    OmniCliTerminalSettings,
    _build_exec_url,
    _resize_payload,
    create_omni_cli_terminal_router,
)
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider


class _Auth(AuthProvider):
    def get_user_id(self, request) -> str | None:  # type: ignore[no-untyped-def]
        return request.headers.get("x-user")


class _PermissionStore:
    def __init__(self, admins: set[str]) -> None:
        self._admins = admins

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins


class _Bridge:
    def __init__(self) -> None:
        self.settings = OmniCliTerminalSettings(
            enabled=True,
            namespace="bytedesk",
            pod_name="omnigent-cli-0",
            container="cli",
            server_url="http://omnigent-server.bytedesk.svc.cluster.local",
            command=("/bin/sh", "-lc", "exec /bin/sh -l"),
            api_base_url="https://kubernetes.default.svc:443",
            token_path=Path("/tmp/token"),
            ca_path=Path("/tmp/ca.crt"),
        )

    async def target_summary(self) -> dict[str, object]:
        return {
            "enabled": True,
            "namespace": "bytedesk",
            "pod_name": "omnigent-cli-0",
            "container": "cli",
            "phase": "Running",
            "server_url": "http://omnigent-server.bytedesk.svc.cluster.local",
            "attach_path": "/v1/admin/omni-cli/terminal/attach",
        }

    async def attach(self, websocket) -> None:  # type: ignore[no-untyped-def]
        await websocket.send_bytes(b"omni-ready")
        await websocket.close()


def _app(admins: set[str]) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(_request, exc: OmnigentError) -> JSONResponse:  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_omni_cli_terminal_router(
            auth_provider=_Auth(),
            permission_store=_PermissionStore(admins),
            bridge=_Bridge(),
        ),
        prefix="/v1",
    )
    return app


def test_status_requires_admin_and_returns_attach_path() -> None:
    client = TestClient(_app({"alice"}))

    denied = client.get("/v1/admin/omni-cli/terminal", headers={"x-user": "bob"})
    assert denied.status_code == 403

    ok = client.get("/v1/admin/omni-cli/terminal", headers={"x-user": "alice"})
    assert ok.status_code == 200
    assert ok.json()["attach_path"] == "/v1/admin/omni-cli/terminal/attach"
    assert ok.json()["pod_name"] == "omnigent-cli-0"


def test_websocket_requires_admin_before_attach() -> None:
    client = TestClient(_app({"alice"}))

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/v1/admin/omni-cli/terminal/attach",
            headers={"x-user": "bob"},
        ):
            pass
    assert exc_info.value.code == 1008


def test_websocket_admin_attaches_to_bridge() -> None:
    client = TestClient(_app({"alice"}))

    with client.websocket_connect(
        "/v1/admin/omni-cli/terminal/attach",
        headers={"x-user": "alice"},
    ) as ws:
        assert ws.receive_bytes() == b"omni-ready"


def test_exec_url_uses_kubernetes_channel_exec_contract() -> None:
    settings = OmniCliTerminalSettings(
        enabled=True,
        namespace="bytedesk",
        pod_name="omnigent-cli-0",
        container="cli",
        server_url="http://omnigent-server.bytedesk.svc.cluster.local",
        command=("/bin/sh", "-lc", "exec /bin/sh -l"),
        api_base_url="https://kubernetes.default.svc:443",
        token_path=Path("/tmp/token"),
        ca_path=Path("/tmp/ca.crt"),
    )

    url = _build_exec_url(settings)

    assert url.startswith("wss://kubernetes.default.svc:443/api/v1/namespaces/bytedesk/pods/")
    assert "/omnigent-cli-0/exec?" in url
    assert "container=cli" in url
    assert "stdin=true" in url
    assert "tty=true" in url
    assert "command=%2Fbin%2Fsh" in url


def test_resize_payload_matches_kubernetes_exec_channel_shape() -> None:
    assert _resize_payload('{"type":"resize","cols":120,"rows":40}') == (
        b'{"Width":120,"Height":40}'
    )
    assert _resize_payload('{"type":"resize","cols":0,"rows":40}') is None
    assert _resize_payload('{"type":"ping"}') is None
