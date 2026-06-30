"""E2E: connector catalog transitions into a grouped, scrollable provider detail page."""

from __future__ import annotations

import json
import re
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, Route, expect


_REPO_ROOT = Path(__file__).resolve().parents[3]
_AP_WEB_DIR = _REPO_ROOT / "ap-web"

GOOGLE_SERVICE_KEYS = [
    "workspace",
    "gmail",
    "calendar",
    "chat",
    "drive",
    "docs",
    "sheets",
    "slides",
    "forms",
    "keep",
    "meet",
    "sites",
    "tasks",
    "admin_settings",
    "admin_directory",
    "cloud_identity",
    "people",
    "domain_shared_contacts",
    "contact_delegation",
    "groups_settings",
    "groups_migration",
    "license_manager",
    "reports",
    "alert_center",
    "data_transfer",
    "reseller",
    "cloud_search",
    "drive_activity",
    "drive_labels",
    "apps_script",
    "workspace_add_ons",
    "drive_apps",
    "marketplace",
    "gmail_settings",
    "email_audit",
    "postmaster_tools",
    "chrome_browser_cloud_management",
    "chrome_enrollment_tokens",
    "chrome_printer_management",
    "vault",
    "vertex_ai",
]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def connector_ui_base_url(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    override = request.config.getoption("--ui-base-url")
    if override:
        yield str(override).rstrip("/")
        return

    request.getfixturevalue("built_spa")

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("connectors_vite") / "vite.log"
    log_handle = open(log_path, "w")  # noqa: SIM115 - closed in fixture teardown.
    proc = subprocess.Popen(
        [
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--strictPort",
        ],
        cwd=_AP_WEB_DIR,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    deadline = time.monotonic() + 45
    ready = False
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last_error = f"vite exited early with code {proc.returncode}"
            break
        try:
            response = httpx.get(base_url, timeout=1)
            if response.status_code == 200:
                ready = True
                break
            last_error = f"HTTP {response.status_code}"
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)

    if not ready:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()
        pytest.fail(
            f"Vite did not start at {base_url} within 45s (last_error={last_error}).\n"
            f"Log at {log_path}:\n{log_path.read_text()[-3000:]}"
        )

    try:
        yield base_url
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


def _json(route: Route, body: dict[str, object]) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _service_name(key: str) -> str:
    return key.replace("_", " ").title()


def _service(key: str) -> dict[str, object]:
    return {
        "key": key,
        "name": _service_name(key),
        "description": "",
        "scopes": [],
        "toolMounts": [],
        "tools": [
            {
                "key": "read",
                "name": f"Read {_service_name(key)}",
                "description": "",
                "mcpTool": f"{key}_read",
                "scopes": [],
            },
            {
                "key": "search",
                "name": f"Search {_service_name(key)}",
                "description": "",
                "mcpTool": f"{key}_search",
                "scopes": [],
            },
        ],
    }


def _google_connection() -> dict[str, object]:
    return {
        "id": "conn_google",
        "provider": "google_workspace",
        "displayName": "ByteDesk Workspace",
        "authType": "google_domain_wide_delegation",
        "status": "connected",
        "scopes": [],
        "metadata": {},
        "secretPresent": True,
        "lastHealthStatus": "healthy",
        "lastHealthAt": 1,
        "lastError": None,
        "createdAt": 1,
        "updatedAt": 1,
        "version": 1,
        "services": [
            {
                "id": f"svc_{key}",
                "connectionId": "conn_google",
                "serviceKey": key,
                "enabled": True,
                "status": "ready",
                "scopes": [],
                "metadata": {},
                "updatedAt": 1,
                "version": 1,
            }
            for key in GOOGLE_SERVICE_KEYS
        ],
        "grants": [],
    }


def _catalog() -> dict[str, object]:
    return {
        "providers": [
            {
                "provider": "atlassian",
                "name": "Atlassian",
                "description": "Jira and Confluence tools.",
                "auth": {"type": "oauth_3lo", "scopes": [], "docsUrl": None, "setupFields": []},
                "services": [
                    {
                        "key": "jira",
                        "name": "Jira",
                        "description": "",
                        "scopes": [],
                        "toolMounts": [],
                        "tools": [],
                    }
                ],
                "connections": [],
            },
            {
                "provider": "google_workspace",
                "name": "Google Workspace",
                "description": "Google Workspace service tools.",
                "auth": {
                    "type": "google_domain_wide_delegation",
                    "scopes": [],
                    "docsUrl": "https://developers.google.com/workspace",
                    "setupFields": [],
                },
                "services": [_service(key) for key in GOOGLE_SERVICE_KEYS],
                "connections": [_google_connection()],
            },
        ]
    }


def _stub_connector_routes(page: Page) -> None:
    page.route(
        "**/v1/info",
        lambda route: _json(
            route,
            {
                "accounts_enabled": False,
                "login_url": None,
                "needs_setup": False,
                "databricks_features": False,
                "managed_sandboxes_enabled": False,
                "sandbox_provider": None,
                "omni_cli_terminal_enabled": True,
            },
        ),
    )
    page.route("**/v1/me", lambda route: _json(route, {"user_id": "alice"}))
    page.route("**/v1/connectors/catalog", lambda route: _json(route, _catalog()))
    page.route(
        "**/v1/agents?*",
        lambda route: _json(
            route,
            {
                "data": [
                    {
                        "id": "ag_maya",
                        "name": "chief-of-staff",
                        "display_name": "Maya Chen",
                        "description": None,
                        "harness": "codex",
                        "skills": [],
                    }
                ]
            },
        ),
    )
    page.route("**/v1/sessions?*", lambda route: _json(route, {"data": []}))
    page.route(
        "**/v1/connectors/connections/*/agent-grants",
        lambda route: _json(route, {"grants": []}),
    )


def test_connectors_catalog_drills_into_scrollable_grouped_provider(
    page: Page, connector_ui_base_url: str
) -> None:
    _stub_connector_routes(page)
    page.set_viewport_size({"width": 900, "height": 520})

    page.goto(f"{connector_ui_base_url}/connectors")

    expect(page.get_by_role("heading", name="Connectors")).to_be_visible(timeout=30_000)
    google_card = page.locator("section").filter(has_text="Google Workspace")
    expect(google_card.get_by_text("41 services")).to_be_visible()
    expect(google_card.get_by_text("82 actions")).to_be_visible()
    expect(google_card.get_by_role("button", name="Connect")).to_have_count(0)

    google_card.get_by_role("link", name="Configure").click()

    expect(page).to_have_url(re.compile(r"/connectors/google_workspace$"))
    expect(page.get_by_role("link", name="Connectors")).to_be_visible()
    expect(page.get_by_role("link", name=re.compile("Back"))).to_be_visible()
    expect(page.get_by_text("Drive & Content").first).to_be_visible()
    expect(page.get_by_text("Admin & Directory").first).to_be_visible()
    expect(page.get_by_text("Automation & AI").first).to_be_visible()

    scroll = page.get_by_test_id("connector-page-scroll")
    expect(scroll).to_be_visible()
    assert scroll.evaluate(
        """(el) => el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY === 'auto'"""
    )

    scroll.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
    expect(page.get_by_role("button", name=re.compile("Grant"))).to_be_visible()
