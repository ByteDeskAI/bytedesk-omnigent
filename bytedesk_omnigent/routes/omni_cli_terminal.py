"""Admin Omni CLI terminal backed by Kubernetes exec.

This route is deliberately separate from session terminals. It does not create
or attach to agent resources; it opens an operator shell in the dedicated
``omnigent-cli`` pod so admins can run ``omni`` commands from the Omnigent UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, WebSocketException
from starlette import status

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    RESERVED_USER_LOCAL,
    AuthProvider,
    env_var_is_truthy,
    local_single_user_enabled,
)
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore

logger = logging.getLogger(__name__)

_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_NAMESPACE_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
_CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
_K8S_EXEC_SUBPROTOCOL = "v4.channel.k8s.io"
_WS_CLOSE_INTERNAL_ERROR = 4500
_WS_CLOSE_NOT_FOUND = 4404


@dataclass(frozen=True)
class OmniCliTerminalSettings:
    """Runtime settings for the admin CLI terminal target."""

    enabled: bool
    namespace: str
    pod_name: str
    container: str
    server_url: str
    command: tuple[str, ...]
    api_base_url: str
    token_path: Path
    ca_path: Path

    @classmethod
    def from_env(cls) -> OmniCliTerminalSettings:
        """Load settings from environment and service-account defaults."""
        server_url = os.environ.get(
            "OMNIGENT_OMNI_CLI_SERVER_URL",
            "http://omnigent-server.bytedesk.svc.cluster.local",
        ).strip()
        shell = os.environ.get("OMNIGENT_OMNI_CLI_SHELL", "/bin/sh").strip() or "/bin/sh"
        command = (
            shell,
            "-lc",
            f"export OMNIGENT_SERVER_URL={shlex.quote(server_url)}; exec {shlex.quote(shell)} -l",
        )
        return cls(
            enabled=env_var_is_truthy("OMNIGENT_OMNI_CLI_TERMINAL_ENABLED"),
            namespace=os.environ.get("OMNIGENT_OMNI_CLI_NAMESPACE", _read_namespace()).strip()
            or "default",
            pod_name=os.environ.get("OMNIGENT_OMNI_CLI_POD_NAME", "omnigent-cli-0").strip()
            or "omnigent-cli-0",
            container=os.environ.get("OMNIGENT_OMNI_CLI_CONTAINER", "cli").strip() or "cli",
            server_url=server_url,
            command=command,
            api_base_url=_kubernetes_api_base_url(),
            token_path=Path(os.environ.get("OMNIGENT_KUBERNETES_TOKEN_PATH", _TOKEN_PATH)),
            ca_path=Path(os.environ.get("OMNIGENT_KUBERNETES_CA_PATH", _CA_PATH)),
        )


class OmniCliTerminalBridge(Protocol):
    """Bridge interface used by the route and tests."""

    @property
    def settings(self) -> OmniCliTerminalSettings:
        """Current terminal target settings."""
        ...

    async def target_summary(self) -> dict[str, object]:
        """Return target status metadata for the admin page."""
        ...

    async def attach(self, websocket: WebSocket) -> None:
        """Bridge an accepted browser websocket to the target terminal."""
        ...


class KubernetesOmniCliTerminalBridge:
    """Kubernetes exec bridge for the dedicated Omni CLI pod."""

    def __init__(self, settings: OmniCliTerminalSettings | None = None) -> None:
        self._settings = settings or OmniCliTerminalSettings.from_env()

    @property
    def settings(self) -> OmniCliTerminalSettings:
        return self._settings

    async def target_summary(self) -> dict[str, object]:
        """Read the target pod and return a small status payload."""
        if not self._settings.enabled:
            return self._status_payload(enabled=False, phase=None)
        pod = await self._get_target_pod()
        phase = str(pod.get("status", {}).get("phase") or "Unknown")
        return self._status_payload(enabled=True, phase=phase)

    async def attach(self, websocket: WebSocket) -> None:
        """Bridge an accepted browser websocket to Kubernetes exec."""
        if not self._settings.enabled:
            await websocket.close(code=_WS_CLOSE_NOT_FOUND, reason="Omni CLI terminal disabled")
            return

        try:
            pod = await self._get_target_pod()
            phase = str(pod.get("status", {}).get("phase") or "Unknown")
            if phase != "Running":
                await websocket.close(
                    code=_WS_CLOSE_NOT_FOUND,
                    reason=f"Omni CLI pod is {phase.lower()}",
                )
                return
            await _bridge_kubernetes_exec(websocket, self._settings)
        except OmnigentError as exc:
            logger.warning("Omni CLI terminal attach rejected: %s", exc.message)
            with contextlib.suppress(RuntimeError):
                await websocket.close(
                    code=(
                        _WS_CLOSE_NOT_FOUND
                        if exc.code == ErrorCode.NOT_FOUND
                        else _WS_CLOSE_INTERNAL_ERROR
                    ),
                    reason=exc.message,
                )
        except Exception:
            logger.exception("Omni CLI terminal bridge failed")
            with contextlib.suppress(RuntimeError):
                await websocket.close(
                    code=_WS_CLOSE_INTERNAL_ERROR,
                    reason="Omni CLI terminal bridge failed",
                )

    def _status_payload(self, *, enabled: bool, phase: str | None) -> dict[str, object]:
        return {
            "enabled": enabled,
            "namespace": self._settings.namespace,
            "pod_name": self._settings.pod_name,
            "container": self._settings.container,
            "phase": phase,
            "server_url": self._settings.server_url,
            "attach_path": "/v1/admin/omni-cli/terminal/attach",
        }

    async def _get_target_pod(self) -> dict[str, object]:
        token = _read_token(self._settings.token_path)
        url = (
            f"{self._settings.api_base_url}/api/v1/namespaces/"
            f"{self._settings.namespace}/pods/{self._settings.pod_name}"
        )
        async with httpx.AsyncClient(
            verify=_httpx_verify(self._settings.ca_path),
            timeout=10.0,
            trust_env=False,
        ) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 404:
            raise OmnigentError(
                "Omni CLI pod not found",
                code=ErrorCode.NOT_FOUND,
            )
        if response.status_code >= 400:
            raise OmnigentError(
                f"Kubernetes API rejected Omni CLI pod lookup ({response.status_code})",
                code=ErrorCode.INTERNAL_ERROR,
            )
        body = response.json()
        if not isinstance(body, dict):
            raise OmnigentError(
                "Kubernetes API returned an invalid pod response",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return body


def create_omni_cli_terminal_router(
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    bridge: OmniCliTerminalBridge | None = None,
) -> APIRouter:
    """Create admin Omni CLI terminal routes."""
    router = APIRouter()
    terminal_bridge = bridge or KubernetesOmniCliTerminalBridge()

    @router.get("/admin/omni-cli/terminal")
    async def get_omni_cli_terminal(request: Request) -> dict[str, object]:
        user_id = await _require_admin_http(request, auth_provider, permission_store)
        logger.info("Omni CLI terminal status requested by %s", _redact_user(user_id))
        return await terminal_bridge.target_summary()

    @router.websocket("/admin/omni-cli/terminal/attach")
    async def attach_omni_cli_terminal(websocket: WebSocket) -> None:
        user_id = await _require_admin_ws(websocket, auth_provider, permission_store)
        logger.info("Omni CLI terminal attach requested by %s", _redact_user(user_id))
        await websocket.accept()
        await terminal_bridge.attach(websocket)

    return router


async def _require_admin_http(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str | None:
    user_id = get_user_id(request, auth_provider)
    await _require_admin(user_id, permission_store)
    return user_id


async def _require_admin_ws(
    websocket: WebSocket,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str | None:
    user_id = get_user_id(websocket, auth_provider)
    try:
        await _require_admin(user_id, permission_store)
    except OmnigentError as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=exc.message,
        ) from exc
    return user_id


async def _require_admin(
    user_id: str | None,
    permission_store: PermissionStore | None,
) -> None:
    """Require server admin, preserving explicit local single-user mode."""
    if permission_store is None:
        return
    if user_id == RESERVED_USER_LOCAL and local_single_user_enabled():
        return
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
    if not is_admin:
        raise OmnigentError(
            "Admin privileges required to open the Omni CLI terminal",
            code=ErrorCode.FORBIDDEN,
        )


async def _bridge_kubernetes_exec(
    browser_ws: WebSocket,
    settings: OmniCliTerminalSettings,
) -> None:
    """Bridge browser terminal frames to Kubernetes exec frames."""
    import websockets
    from websockets.exceptions import ConnectionClosed

    token = _read_token(settings.token_path)
    exec_url = _build_exec_url(settings)
    ssl_context = _ssl_context(settings.ca_path)
    async with websockets.connect(
        exec_url,
        subprotocols=[_K8S_EXEC_SUBPROTOCOL],
        additional_headers={"Authorization": f"Bearer {token}"},
        ssl=ssl_context,
        compression=None,
        proxy=None,
    ) as kube_ws:

        async def browser_to_kube() -> None:
            try:
                while True:
                    msg = await browser_ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return
                    text = msg.get("text")
                    data = msg.get("bytes")
                    if text is not None:
                        resize_payload = _resize_payload(text)
                        if resize_payload is not None:
                            await kube_ws.send(b"\x04" + resize_payload)
                    elif data is not None:
                        await kube_ws.send(b"\x00" + data)
            except (WebSocketDisconnect, ConnectionClosed):
                return

        async def kube_to_browser() -> None:
            try:
                while True:
                    msg = await kube_ws.recv()
                    if isinstance(msg, str):
                        payload = msg.encode()
                    else:
                        payload = bytes(msg)
                    if not payload:
                        continue
                    channel = payload[0]
                    body = payload[1:]
                    if channel in (1, 2):
                        await browser_ws.send_bytes(body)
                    elif channel == 3:
                        reason = _status_reason(body)
                        await browser_ws.close(reason=reason)
                        return
            except (WebSocketDisconnect, ConnectionClosed):
                return

        b2k = asyncio.create_task(browser_to_kube(), name="omni-cli-browser-to-kube")
        k2b = asyncio.create_task(kube_to_browser(), name="omni-cli-kube-to-browser")
        done, pending = await asyncio.wait({b2k, k2b}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc


def _build_exec_url(settings: OmniCliTerminalSettings) -> str:
    base = settings.api_base_url
    if base.startswith("https://"):
        ws_base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        ws_base = "ws://" + base.removeprefix("http://")
    else:
        ws_base = base
    params: list[tuple[str, str]] = [
        ("container", settings.container),
        ("stdin", "true"),
        ("stdout", "true"),
        ("stderr", "true"),
        ("tty", "true"),
    ]
    params.extend(("command", part) for part in settings.command)
    qs = urlencode(params)
    return f"{ws_base}/api/v1/namespaces/{settings.namespace}/pods/{settings.pod_name}/exec?{qs}"


def _resize_payload(text: str) -> bytes | None:
    try:
        ctl = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(ctl, dict) or ctl.get("type") != "resize":
        return None
    try:
        cols = int(ctl["cols"])
        rows = int(ctl["rows"])
    except (KeyError, TypeError, ValueError):
        return None
    if cols <= 0 or rows <= 0:
        return None
    return json.dumps({"Width": cols, "Height": rows}, separators=(",", ":")).encode()


def _status_reason(body: bytes) -> str:
    if not body:
        return "terminal exited"
    try:
        status_body = json.loads(body.decode(errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return "terminal exited"
    if isinstance(status_body, dict):
        message = status_body.get("message")
        if isinstance(message, str) and message.strip():
            return message[:120]
    return "terminal exited"


def _read_namespace() -> str:
    with contextlib.suppress(OSError):
        return _NAMESPACE_PATH.read_text(encoding="utf-8").strip()
    return "default"


def _kubernetes_api_base_url() -> str:
    raw = os.environ.get("OMNIGENT_KUBERNETES_API_URL", "").strip()
    if raw:
        return raw.rstrip("/")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
    return f"https://{host}:{port}".rstrip("/")


def _read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OmnigentError(
            "Kubernetes service-account token is unavailable",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if not token:
        raise OmnigentError(
            "Kubernetes service-account token is empty",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return token


def _httpx_verify(ca_path: Path) -> str | bool:
    return str(ca_path) if ca_path.is_file() else True


def _ssl_context(ca_path: Path) -> ssl.SSLContext | None:
    if not ca_path.is_file():
        return None
    return ssl.create_default_context(cafile=str(ca_path))


def _redact_user(user_id: str | None) -> str:
    if user_id is None:
        return "anonymous"
    if len(user_id) <= 4:
        return f"{user_id[:1]}***"
    return f"{user_id[:3]}***(len={len(user_id)})"


__all__ = [
    "KubernetesOmniCliTerminalBridge",
    "OmniCliTerminalSettings",
    "_build_exec_url",
    "_resize_payload",
    "create_omni_cli_terminal_router",
]
