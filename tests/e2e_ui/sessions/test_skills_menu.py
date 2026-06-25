"""E2E: operators can reach Skills from the bottom-left menu."""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect


def _json(route: Route, body: dict[str, object]) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _stub_skills_routes(page: Page) -> None:
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
                "omni_cli_terminal_enabled": False,
            },
        ),
    )
    page.route("**/v1/me", lambda route: _json(route, {"user_id": "local"}))
    page.route(
        "**/v1/agents?limit=100",
        lambda route: _json(
            route,
            {
                "object": "list",
                "data": [
                    {
                        "id": "ag_demo",
                        "name": "demo",
                        "display_name": "Demo",
                        "description": "Demo agent.",
                        "harness": "codex",
                        "skills": [],
                    }
                ],
                "first_id": "ag_demo",
                "last_id": "ag_demo",
                "has_more": False,
            },
        ),
    )
    page.route(
        "**/v1/sessions?limit=100&kind=any",
        lambda route: _json(route, {"object": "list", "data": [], "has_more": False}),
    )
    page.route(
        "**/v1/skills/sources",
        lambda route: _json(
            route,
            {
                "object": "skill_source.list",
                "data": [
                    {
                        "id": "skills",
                        "label": "Agent Skills CLI",
                        "kind": "named_adapter",
                        "supports_search": True,
                        "supports_preview": True,
                        "high_risk": False,
                    }
                ],
            },
        ),
    )
    page.route(
        "**/v1/skills/installed",
        lambda route: _json(route, {"object": "installed_skill.list", "data": []}),
    )


def test_local_operator_menu_opens_skills(page: Page, live_server: str) -> None:
    """The non-accounts bottom-left menu links to the Skills page."""

    _stub_skills_routes(page)

    page.goto(f"{live_server}/")
    page.get_by_role("button", name="Omnigent").click()
    page.get_by_role("menuitem", name="Skills").click()

    expect(page).to_have_url(f"{live_server}/skills")
    expect(page.get_by_role("heading", name="Skills")).to_be_visible(timeout=30_000)
    expect(page.get_by_text("No skills installed.")).to_be_visible()

