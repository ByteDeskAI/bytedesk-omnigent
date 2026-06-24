"""E2E: operators can reach the standalone Omni CLI terminal from the bottom-left menu."""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect


def _json(route: Route, body: dict[str, object]) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _stub_terminal_routes(page: Page, *, accounts_enabled: bool) -> None:
    page.route(
        "**/v1/info",
        lambda route: _json(
            route,
            {
                "accounts_enabled": accounts_enabled,
                "login_url": "/login" if accounts_enabled else None,
                "needs_setup": False,
                "databricks_features": False,
                "managed_sandboxes_enabled": False,
                "sandbox_provider": None,
                "omni_cli_terminal_enabled": True,
            },
        ),
    )
    page.route("**/v1/me", lambda route: _json(route, {"user_id": "alice"}))
    if accounts_enabled:
        page.route(
            "**/auth/me",
            lambda route: _json(
                route,
                {"id": "alice", "is_admin": True, "created_at": None, "last_login_at": None},
            ),
        )
    page.route(
        "**/v1/admin/omni-cli/terminal",
        lambda route: _json(
            route,
            {
                "enabled": False,
                "namespace": "bytedesk",
                "pod_name": "omnigent-cli-0",
                "container": "cli",
                "phase": None,
                "server_url": "http://omnigent-server.bytedesk.svc.cluster.local",
                "attach_path": "/v1/admin/omni-cli/terminal/attach",
            },
        ),
    )


def test_local_operator_menu_opens_omni_cli_terminal(page: Page, live_server: str) -> None:
    """The non-accounts bottom-left menu links to the standalone Omni CLI terminal page."""

    _stub_terminal_routes(page, accounts_enabled=False)

    page.goto(f"{live_server}/")
    page.get_by_role("button", name="Omnigent").click()
    page.get_by_role("menuitem", name="Terminal").click()

    expect(page).to_have_url(f"{live_server}/terminal")
    expect(page.get_by_role("heading", name="Terminal")).to_be_visible(timeout=30_000)
    expect(page.get_by_text("Terminal is disabled.")).to_be_visible()


def test_admin_account_menu_opens_omni_cli_terminal(page: Page, live_server: str) -> None:
    """The account-mode bottom-left menu links to the standalone Omni CLI terminal page."""

    _stub_terminal_routes(page, accounts_enabled=True)

    page.goto(f"{live_server}/")
    page.get_by_role("button", name="alice").click()
    page.get_by_role("menuitem", name="Terminal").click()

    expect(page).to_have_url(f"{live_server}/terminal")
    expect(page.get_by_role("heading", name="Terminal")).to_be_visible(timeout=30_000)
    expect(page.get_by_text("Terminal is disabled.")).to_be_visible()
