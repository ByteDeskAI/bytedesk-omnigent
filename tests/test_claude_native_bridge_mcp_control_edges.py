"""Edge tests for claude_native_bridge MCP control HTTP ingress helpers."""

from __future__ import annotations

import json
import queue
import threading
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from omnigent import claude_native_bridge


@pytest.fixture(autouse=True)
def _trusted_bridge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path)


def _start_control_server(
    bridge_dir: Path,
    token: str = "control-token",
) -> tuple[ThreadingHTTPServer, queue.Queue[dict[str, Any] | None], str]:
    notification_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
    httpd = claude_native_bridge._start_http_ingress(bridge_dir, token, notification_queue)
    host, port = httpd.server_address
    return httpd, notification_queue, f"http://{host}:{port}"


def test_start_http_ingress_writes_server_file(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    httpd, _queue, _url = _start_control_server(bridge_dir)
    try:
        server_info = json.loads((bridge_dir / claude_native_bridge._SERVER_FILE).read_text())
        assert server_info["token"] == "control-token"
        assert server_info["url"].startswith("http://127.0.0.1:")
        assert server_info["pid"] > 0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_control_handler_health_and_tools_changed(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    httpd, notification_queue, base_url = _start_control_server(bridge_dir)
    try:
        health = urllib.request.urlopen(f"{base_url}/health", timeout=2.0)
        assert health.status == HTTPStatus.OK
        assert json.loads(health.read()) == {"status": "ok"}

        with pytest.raises(urllib.error.HTTPError) as not_found:
            urllib.request.urlopen(f"{base_url}/missing", timeout=2.0)
        assert not_found.value.code == HTTPStatus.NOT_FOUND

        bad_req = urllib.request.Request(
            f"{base_url}/tools-changed",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer wrong"},
        )
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(bad_req, timeout=2.0)
        assert unauthorized.value.code == HTTPStatus.UNAUTHORIZED

        ok_req = urllib.request.Request(
            f"{base_url}/tools-changed",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer control-token"},
        )
        ok_resp = urllib.request.urlopen(ok_req, timeout=2.0)
        assert ok_resp.status == HTTPStatus.OK
        assert json.loads(ok_resp.read()) == {"ok": True}

        notification = notification_queue.get(timeout=2.0)
        assert notification is not None
        assert notification["method"] == "notifications/tools/list_changed"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_handler_factory_post_unknown_path_returns_not_found(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    notification_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
    handler_cls = claude_native_bridge._handler_factory("token", notification_queue)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/other",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer token"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc.value.code == HTTPStatus.NOT_FOUND
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


def test_serve_mcp_exits_when_config_missing_token(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    claude_native_bridge._write_json_file(
        bridge_dir / claude_native_bridge._CONFIG_FILE,
        {"token": ""},
    )

    with pytest.raises(SystemExit, match="missing token"):
        claude_native_bridge._serve_mcp(bridge_dir)


def test_serve_mcp_exits_when_config_file_missing(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    with pytest.raises(SystemExit, match="bridge config missing"):
        claude_native_bridge._serve_mcp(bridge_dir)
