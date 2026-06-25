"""Edge and seam tests for Kubernetes-backed Omni CLI terminal routes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bytedesk_omnigent.routes.omni_cli_terminal import (
    KubernetesOmniCliTerminalBridge,
    OmniCliTerminalSettings,
    _bridge_kubernetes_exec,
    _build_exec_url,
    _httpx_verify,
    _kubernetes_api_base_url,
    _read_namespace,
    _read_token,
    _redact_user,
    _resize_payload,
    _ssl_context,
    _status_reason,
    create_omni_cli_terminal_router,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider


def _settings(
    tmp_path: Path,
    *,
    enabled: bool = True,
    api_base_url: str = "https://kubernetes.default.svc:443",
) -> OmniCliTerminalSettings:
    token = tmp_path / "token"
    token.write_text("cluster-token", encoding="utf-8")
    ca = tmp_path / "ca.crt"
    ca.write_text("ca", encoding="utf-8")
    return OmniCliTerminalSettings(
        enabled=enabled,
        namespace="bytedesk",
        pod_name="omnigent-cli-0",
        container="cli",
        server_url="http://omnigent-server.bytedesk.svc.cluster.local",
        command=("/bin/sh", "-lc", "exec /bin/sh -l"),
        api_base_url=api_base_url,
        token_path=token,
        ca_path=ca,
    )


class _Auth(AuthProvider):
    def get_user_id(self, request) -> str | None:  # type: ignore[no-untyped-def]
        return request.headers.get("x-user")


class _PermissionStore:
    def __init__(self, admins: set[str]) -> None:
        self._admins = admins

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins


def test_settings_from_env_reads_kubernetes_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_OMNI_CLI_TERMINAL_ENABLED", "true")
    monkeypatch.setenv("OMNIGENT_OMNI_CLI_NAMESPACE", "prod")
    monkeypatch.setenv("OMNIGENT_OMNI_CLI_POD_NAME", "omnigent-cli-1")
    monkeypatch.setenv("OMNIGENT_OMNI_CLI_CONTAINER", "shell")
    monkeypatch.setenv("OMNIGENT_OMNI_CLI_SERVER_URL", "http://server.local")
    monkeypatch.setenv("OMNIGENT_OMNI_CLI_SHELL", "/bin/bash")
    monkeypatch.setenv("OMNIGENT_KUBERNETES_API_URL", "https://k8s.example:6443")
    monkeypatch.setenv("OMNIGENT_KUBERNETES_TOKEN_PATH", str(tmp_path / "tok"))
    monkeypatch.setenv("OMNIGENT_KUBERNETES_CA_PATH", str(tmp_path / "ca"))
    (tmp_path / "tok").write_text("t", encoding="utf-8")

    settings = OmniCliTerminalSettings.from_env()

    assert settings.enabled is True
    assert settings.namespace == "prod"
    assert settings.pod_name == "omnigent-cli-1"
    assert settings.container == "shell"
    assert settings.server_url == "http://server.local"
    assert settings.api_base_url == "https://k8s.example:6443"
    assert settings.command[0] == "/bin/bash"
    assert "OMNIGENT_SERVER_URL" in settings.command[2]


def test_read_namespace_and_api_base_url(monkeypatch, tmp_path: Path) -> None:
    ns_file = tmp_path / "namespace"
    ns_file.write_text("  staging  \n", encoding="utf-8")
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.omni_cli_terminal._NAMESPACE_PATH",
        ns_file,
    )
    assert _read_namespace() == "staging"

    monkeypatch.delenv("OMNIGENT_KUBERNETES_API_URL", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "api.internal")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "8443")
    assert _kubernetes_api_base_url() == "https://api.internal:8443"


def test_read_token_and_tls_helpers(tmp_path: Path, monkeypatch) -> None:
    token_path = tmp_path / "token"
    token_path.write_text(" bearer-token \n", encoding="utf-8")
    ca_path = tmp_path / "ca.crt"
    ca_path.write_text("ca", encoding="utf-8")
    assert _read_token(token_path) == "bearer-token"
    assert _httpx_verify(ca_path) == str(ca_path)
    import ssl

    monkeypatch.setattr(
        ssl, "create_default_context", lambda **_kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )
    assert _ssl_context(ca_path) is not None
    assert _httpx_verify(tmp_path / "missing.crt") is True
    assert _ssl_context(tmp_path / "missing.crt") is None

    with pytest.raises(OmnigentError) as missing:
        _read_token(tmp_path / "nope")
    assert missing.value.code == ErrorCode.INTERNAL_ERROR

    empty = tmp_path / "empty"
    empty.write_text("   ", encoding="utf-8")
    with pytest.raises(OmnigentError) as blank:
        _read_token(empty)
    assert "empty" in blank.value.message


def test_status_reason_and_resize_payload_edges() -> None:
    assert _status_reason(b"") == "terminal exited"
    assert _status_reason(b"not-json") == "terminal exited"
    assert _status_reason(json.dumps({"message": "OOMKilled"}).encode()) == "OOMKilled"
    assert _status_reason(json.dumps({"message": "  "}).encode()) == "terminal exited"

    assert _resize_payload("not-json") is None
    assert _resize_payload('{"type":"resize","cols":"x","rows":1}') is None
    assert _build_exec_url(
        _settings(Path("/tmp"), api_base_url="http://kubernetes.default.svc:8080")
    ).startswith("ws://kubernetes.default.svc:8080/")
    assert _build_exec_url(_settings(Path("/tmp"), api_base_url="wss://already-ws")).startswith(
        "wss://already-ws/"
    )


def test_redact_user_masks_short_and_long_ids() -> None:
    assert _redact_user(None) == "anonymous"
    assert _redact_user("ab") == "a***"
    assert _redact_user("alice-admin") == "ali***(len=11)"


@pytest.mark.asyncio
async def test_kubernetes_bridge_target_summary_disabled(tmp_path: Path) -> None:
    bridge = KubernetesOmniCliTerminalBridge(_settings(tmp_path, enabled=False))
    summary = await bridge.target_summary()
    assert summary["enabled"] is False
    assert summary["phase"] is None


@pytest.mark.asyncio
async def test_kubernetes_bridge_get_target_pod_success_and_errors(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    bridge = KubernetesOmniCliTerminalBridge(settings)

    class _Response:
        def __init__(self, status_code: int, body: object) -> None:
            self.status_code = status_code
            self._body = body

        def json(self) -> object:
            return self._body

    class _Client:
        def __init__(self, response: _Response) -> None:
            self._response = response

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str, headers: dict[str, str]) -> _Response:
            assert headers["Authorization"] == "Bearer cluster-token"
            return self._response

    monkeypatch.setattr(
        "bytedesk_omnigent.routes.omni_cli_terminal.httpx.AsyncClient",
        lambda **_kwargs: _Client(_Response(200, {"status": {"phase": "Running"}})),
    )
    pod = await bridge._get_target_pod()
    assert pod["status"]["phase"] == "Running"

    monkeypatch.setattr(
        "bytedesk_omnigent.routes.omni_cli_terminal.httpx.AsyncClient",
        lambda **_kwargs: _Client(_Response(404, {})),
    )
    with pytest.raises(OmnigentError) as not_found:
        await bridge._get_target_pod()
    assert not_found.value.code == ErrorCode.NOT_FOUND

    monkeypatch.setattr(
        "bytedesk_omnigent.routes.omni_cli_terminal.httpx.AsyncClient",
        lambda **_kwargs: _Client(_Response(500, {})),
    )
    with pytest.raises(OmnigentError) as server:
        await bridge._get_target_pod()
    assert server.value.code == ErrorCode.INTERNAL_ERROR

    monkeypatch.setattr(
        "bytedesk_omnigent.routes.omni_cli_terminal.httpx.AsyncClient",
        lambda **_kwargs: _Client(_Response(200, [])),
    )
    with pytest.raises(OmnigentError) as invalid:
        await bridge._get_target_pod()
    assert "invalid pod response" in invalid.value.message


@pytest.mark.asyncio
async def test_kubernetes_bridge_attach_disabled_and_non_running(
    monkeypatch, tmp_path: Path
) -> None:
    class _WS:
        def __init__(self) -> None:
            self.closed: tuple[int, str] | None = None

        async def close(self, *, code: int, reason: str) -> None:
            self.closed = (code, reason)

    disabled = KubernetesOmniCliTerminalBridge(_settings(tmp_path, enabled=False))
    ws = _WS()
    await disabled.attach(ws)  # type: ignore[arg-type]
    assert ws.closed == (4404, "Omni CLI terminal disabled")

    enabled = KubernetesOmniCliTerminalBridge(_settings(tmp_path))

    async def _pending_pod() -> dict[str, object]:
        return {"status": {"phase": "Pending"}}

    monkeypatch.setattr(enabled, "_get_target_pod", _pending_pod)
    ws2 = _WS()
    await enabled.attach(ws2)  # type: ignore[arg-type]
    assert ws2.closed == (4404, "Omni CLI pod is pending")


@pytest.mark.asyncio
async def test_kubernetes_bridge_attach_surfaces_not_found_and_internal_errors(
    monkeypatch, tmp_path: Path
) -> None:
    bridge = KubernetesOmniCliTerminalBridge(_settings(tmp_path))

    class _WS:
        def __init__(self) -> None:
            self.closed: tuple[int, str] | None = None

        async def close(self, *, code: int, reason: str) -> None:
            self.closed = (code, reason)

    async def _raise_not_found() -> dict[str, object]:
        raise OmnigentError("missing pod", code=ErrorCode.NOT_FOUND)

    monkeypatch.setattr(bridge, "_get_target_pod", _raise_not_found)
    ws = _WS()
    await bridge.attach(ws)  # type: ignore[arg-type]
    assert ws.closed == (4404, "missing pod")

    async def _raise_internal() -> dict[str, object]:
        raise OmnigentError("api down", code=ErrorCode.INTERNAL_ERROR)

    monkeypatch.setattr(bridge, "_get_target_pod", _raise_internal)
    ws2 = _WS()
    await bridge.attach(ws2)  # type: ignore[arg-type]
    assert ws2.closed == (4500, "api down")

    async def _raise_unexpected() -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(bridge, "_get_target_pod", _raise_unexpected)
    ws3 = _WS()
    await bridge.attach(ws3)  # type: ignore[arg-type]
    assert ws3.closed == (4500, "Omni CLI terminal bridge failed")


@pytest.mark.asyncio
async def test_bridge_kubernetes_exec_forwards_resize_stdout_and_exit(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    settings = OmniCliTerminalSettings(
        enabled=settings.enabled,
        namespace=settings.namespace,
        pod_name=settings.pod_name,
        container=settings.container,
        server_url=settings.server_url,
        command=settings.command,
        api_base_url=settings.api_base_url,
        token_path=settings.token_path,
        ca_path=tmp_path / "missing-ca.crt",
    )
    sent_to_kube: list[bytes] = []
    sent_to_browser: list[bytes] = []
    closed_reason: str | None = None
    recv_calls = 0

    class _KubeWS:
        async def send(self, data: bytes) -> None:
            sent_to_kube.append(data)

        async def recv(self) -> bytes:
            nonlocal recv_calls
            recv_calls += 1
            if recv_calls == 1:
                return b"\x01hello"
            return b"\x03" + json.dumps({"message": "done"}).encode()

    class _Connect:
        async def __aenter__(self) -> _KubeWS:
            return _KubeWS()

        async def __aexit__(self, *_args: object) -> None:
            return None

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *_a, **_kw: _Connect())

    class _BrowserWS:
        async def receive(self) -> dict[str, object]:
            if not sent_to_kube:
                return {"text": json.dumps({"type": "resize", "cols": 80, "rows": 24})}
            if len(sent_to_kube) == 1:
                return {"bytes": b"ls\n"}
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, data: bytes) -> None:
            sent_to_browser.append(data)

        async def close(self, *, reason: str) -> None:
            nonlocal closed_reason
            closed_reason = reason

    await _bridge_kubernetes_exec(_BrowserWS(), settings)  # type: ignore[arg-type]

    assert sent_to_kube[0].startswith(b"\x04")
    assert sent_to_kube[1] == b"\x00ls\n"
    assert sent_to_browser == [b"hello"]
    assert closed_reason == "done"


@pytest.mark.asyncio
async def test_bridge_kubernetes_exec_forwards_stderr_and_string_frames(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    settings = OmniCliTerminalSettings(
        enabled=settings.enabled,
        namespace=settings.namespace,
        pod_name=settings.pod_name,
        container=settings.container,
        server_url=settings.server_url,
        command=settings.command,
        api_base_url=settings.api_base_url,
        token_path=settings.token_path,
        ca_path=tmp_path / "missing-ca.crt",
    )
    sent_to_browser: list[bytes] = []
    recv_calls = 0

    class _KubeWS:
        async def send(self, _data: bytes) -> None:
            return None

        async def recv(self) -> str | bytes:
            nonlocal recv_calls
            recv_calls += 1
            if recv_calls == 1:
                return "\x02err"
            return b"\x03" + json.dumps({"message": "bye"}).encode()

    class _Connect:
        async def __aenter__(self) -> _KubeWS:
            return _KubeWS()

        async def __aexit__(self, *_args: object) -> None:
            return None

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *_a, **_kw: _Connect())

    class _BrowserWS:
        async def receive(self) -> dict[str, object]:
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, data: bytes) -> None:
            sent_to_browser.append(data)

        async def close(self, *, reason: str) -> None:
            return None

    await _bridge_kubernetes_exec(_BrowserWS(), settings)  # type: ignore[arg-type]
    assert sent_to_browser == [b"err"]


@pytest.mark.asyncio
async def test_bridge_kubernetes_exec_skips_empty_payload_and_disconnects(
    monkeypatch, tmp_path: Path
) -> None:
    from starlette.websockets import WebSocketDisconnect

    settings = _settings(tmp_path)
    settings = OmniCliTerminalSettings(
        enabled=settings.enabled,
        namespace=settings.namespace,
        pod_name=settings.pod_name,
        container=settings.container,
        server_url=settings.server_url,
        command=settings.command,
        api_base_url=settings.api_base_url,
        token_path=settings.token_path,
        ca_path=tmp_path / "missing-ca.crt",
    )
    recv_calls = 0

    class _KubeWS:
        async def send(self, _data: bytes) -> None:
            return None

        async def recv(self) -> bytes:
            nonlocal recv_calls
            recv_calls += 1
            if recv_calls == 1:
                return b""
            raise WebSocketDisconnect(code=1000)

    class _Connect:
        async def __aenter__(self) -> _KubeWS:
            return _KubeWS()

        async def __aexit__(self, *_args: object) -> None:
            return None

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *_a, **_kw: _Connect())

    class _BrowserWS:
        async def receive(self) -> dict[str, object]:
            raise WebSocketDisconnect(code=1000)

        async def send_bytes(self, _data: bytes) -> None:
            return None

        async def close(self, *, reason: str) -> None:
            return None

    await _bridge_kubernetes_exec(_BrowserWS(), settings)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bridge_kubernetes_exec_cancels_pending_browser_task(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, api_base_url="http://k8s.local")
    settings = OmniCliTerminalSettings(
        enabled=settings.enabled,
        namespace=settings.namespace,
        pod_name=settings.pod_name,
        container=settings.container,
        server_url=settings.server_url,
        command=settings.command,
        api_base_url=settings.api_base_url,
        token_path=settings.token_path,
        ca_path=tmp_path / "missing-ca.crt",
    )

    class _KubeWS:
        async def send(self, _data: bytes) -> None:
            return None

        async def recv(self) -> bytes:
            return b"\x03" + json.dumps({"message": "done"}).encode()

    class _Connect:
        async def __aenter__(self) -> _KubeWS:
            return _KubeWS()

        async def __aexit__(self, *_args: object) -> None:
            return None

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *_a, **_kw: _Connect())

    class _BrowserWS:
        async def receive(self) -> dict[str, object]:
            await asyncio.Event().wait()
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, _data: bytes) -> None:
            return None

        async def close(self, *, reason: str) -> None:
            return None

    await _bridge_kubernetes_exec(_BrowserWS(), settings)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bridge_kubernetes_exec_reraises_completed_task_error(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, api_base_url="http://k8s.local")
    settings = OmniCliTerminalSettings(
        enabled=settings.enabled,
        namespace=settings.namespace,
        pod_name=settings.pod_name,
        container=settings.container,
        server_url=settings.server_url,
        command=settings.command,
        api_base_url=settings.api_base_url,
        token_path=settings.token_path,
        ca_path=tmp_path / "missing-ca.crt",
    )

    class _KubeWS:
        async def send(self, _data: bytes) -> None:
            return None

        async def recv(self) -> bytes:
            await asyncio.sleep(3600)
            return b""

    class _Connect:
        async def __aenter__(self) -> _KubeWS:
            return _KubeWS()

        async def __aexit__(self, *_args: object) -> None:
            return None

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *_a, **_kw: _Connect())

    class _BrowserWS:
        async def receive(self) -> dict[str, object]:
            raise RuntimeError("browser receive failed")

        async def send_bytes(self, _data: bytes) -> None:
            return None

        async def close(self, *, reason: str) -> None:
            return None

    with pytest.raises(RuntimeError, match="browser receive failed"):
        await _bridge_kubernetes_exec(_BrowserWS(), settings)  # type: ignore[arg-type]


def test_default_kubernetes_bridge_uses_env_settings(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_OMNI_CLI_TERMINAL_ENABLED", "false")
    bridge = KubernetesOmniCliTerminalBridge()
    assert bridge.settings.enabled is False


@pytest.mark.asyncio
async def test_kubernetes_bridge_target_summary_running_and_attach_success(
    monkeypatch, tmp_path: Path
) -> None:
    bridge = KubernetesOmniCliTerminalBridge(_settings(tmp_path))

    async def _running_pod() -> dict[str, object]:
        return {"status": {"phase": "Running"}}

    monkeypatch.setattr(bridge, "_get_target_pod", _running_pod)
    summary = await bridge.target_summary()
    assert summary["phase"] == "Running"

    bridged = {"called": False}

    async def _fake_bridge(_ws, _settings) -> None:
        bridged["called"] = True

    monkeypatch.setattr(
        "bytedesk_omnigent.routes.omni_cli_terminal._bridge_kubernetes_exec",
        _fake_bridge,
    )

    class _WS:
        async def close(self, *, code: int, reason: str) -> None:
            return None

    await bridge.attach(_WS())  # type: ignore[arg-type]
    assert bridged["called"] is True


def test_require_admin_without_permission_store_allows() -> None:
    import asyncio

    from bytedesk_omnigent.routes.omni_cli_terminal import _require_admin

    asyncio.run(_require_admin(None, None))


def test_router_forbidden_when_not_admin(tmp_path: Path) -> None:
    from fastapi.responses import JSONResponse

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
            permission_store=_PermissionStore(set()),
            bridge=KubernetesOmniCliTerminalBridge(_settings(tmp_path, enabled=False)),
        ),
        prefix="/v1",
    )
    client = TestClient(app)
    denied = client.get("/v1/admin/omni-cli/terminal", headers={"x-user": "bob"})
    assert denied.status_code == 403


def test_read_namespace_default_when_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "bytedesk_omnigent.routes.omni_cli_terminal._NAMESPACE_PATH",
        tmp_path / "missing-ns",
    )
    assert _read_namespace() == "default"


def test_resize_payload_rejects_invalid_dimensions() -> None:
    assert _resize_payload('{"type":"resize","cols":1,"rows":-1}') is None
    assert _resize_payload('{"type":"resize","cols":null,"rows":2}') is None


def test_router_auth_edges(monkeypatch, tmp_path: Path) -> None:
    from fastapi.responses import JSONResponse

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
            permission_store=_PermissionStore({"alice"}),
            bridge=KubernetesOmniCliTerminalBridge(_settings(tmp_path, enabled=False)),
        ),
        prefix="/v1",
    )
    client = TestClient(app)

    denied = client.get("/v1/admin/omni-cli/terminal")
    assert denied.status_code == 401

    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "true")
    local_ok = client.get(
        "/v1/admin/omni-cli/terminal",
        headers={"x-user": RESERVED_USER_LOCAL},
    )
    assert local_ok.status_code == 200

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/v1/admin/omni-cli/terminal/attach"):
            pass
    assert exc_info.value.code == 1008
