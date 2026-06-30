"""UI smoke for the Work Force admin route."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUILD_OUTPUT = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"


class _SpaHandler(SimpleHTTPRequestHandler):
    """Serve the built SPA with BrowserRouter fallback to index.html."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(_BUILD_OUTPUT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        requested = (_BUILD_OUTPUT / parsed.path.lstrip("/")).resolve()
        if parsed.path != "/" and not requested.exists():
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _serve_static_spa() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SpaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _register_api_routes(page: Page) -> list[str]:
    """Stub the server API calls the Work Force route needs for hydration."""
    custom_agent_scan_requests: list[str] = []

    def fulfill_json(route, body: dict[str, object]) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle(route) -> None:
        request = route.request
        path = urlparse(request.url).path

        if path == "/v1/info":
            fulfill_json(
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
            )
            return
        if path == "/health":
            fulfill_json(route, {"status": "ok"})
            return
        if path == "/v1/me":
            fulfill_json(route, {"user_id": "local"})
            return
        if path == "/v1/hosts":
            fulfill_json(route, {"data": []})
            return
        if path == "/v1/sessions":
            query = parse_qs(urlparse(request.url).query)
            if query.get("kind") == ["any"]:
                custom_agent_scan_requests.append(request.url)
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body=json.dumps({"detail": "Work Force must not scan custom sessions"}),
                )
                return
            fulfill_json(route, {"data": []})
            return
        if path == "/v1/agents":
            fulfill_json(
                route,
                {
                    "data": [
                        {
                            "id": "ag_backend",
                            "name": "backend-development-lead",
                            "display_name": "Backend Development Lead",
                            "description": "Builds platform code.",
                            "harness": "codex",
                            "skills": [],
                            "department": "Engineering",
                            "title": "Backend Development Lead",
                            "workflow": False,
                            "category": "employee",
                        },
                        {
                            "id": "ag_employee",
                            "name": "platform-developer",
                            "display_name": "Platform Developer",
                            "description": "Builds platform code.",
                            "harness": "codex",
                            "skills": [],
                            "department": "Engineering",
                            "title": "Platform Engineer",
                            "workflow": False,
                            "category": "employee",
                        },
                        {
                            "id": "ag_marketing",
                            "name": "brand-and-creative-director",
                            "display_name": "Brand & Creative Director",
                            "description": "Runs brand and creative.",
                            "harness": "claude-sdk",
                            "skills": [],
                            "department": "Marketing",
                            "title": "Brand & Creative Lead",
                            "workflow": False,
                            "category": "employee",
                        },
                        {
                            "id": "ag_hello",
                            "name": "hello_world",
                            "display_name": "Hello World",
                            "description": "Ad-hoc test agent.",
                            "harness": "openai-agents",
                            "skills": [],
                            "department": None,
                            "title": None,
                            "workflow": False,
                            "category": "employee",
                        },
                        {
                            "id": "ag_system",
                            "name": "polly",
                            "display_name": "Polly",
                            "description": "System router.",
                            "harness": "claude-sdk",
                            "skills": [],
                            "department": None,
                            "title": None,
                            "workflow": False,
                            "category": "system",
                        },
                        {
                            "id": "ag_workflow",
                            "name": "weekly-business-review",
                            "display_name": "Weekly Business Review",
                            "description": "Weekly workflow.",
                            "harness": "claude-sdk",
                            "skills": [],
                            "department": "Operations",
                            "title": "Workflow",
                            "workflow": True,
                            "category": "workflow",
                        },
                    ]
                },
            )
            return
        if path == "/v1/agents/ag_employee/image":
            fulfill_json(
                route,
                {
                    "id": "ag_employee",
                    "name": "platform-developer",
                    "version": 3,
                    "config": {
                        "spec_version": 1,
                        "name": "platform-developer",
                        "executor": {"type": "omnigent", "config": {"harness": "codex"}},
                    },
                    "instructions": "Use the repo rules.\n",
                    "skills": [],
                    "mcp_servers": [],
                    "python_tools": [],
                    "typescript_tools": [],
                    "sub_agents": [],
                    "sot_tier": "migrated",
                },
            )
            return
        if path.startswith("/v1/"):
            fulfill_json(route, {})
            return
        route.continue_()

    page.route("**/*", handle)
    return custom_agent_scan_requests


def test_work_force_page_renders_agent_admin_shell(page: Page, built_spa: None) -> None:
    """The Work Force route shows agent sections and editor tabs."""
    del built_spa

    custom_agent_scan_requests = _register_api_routes(page)
    with _serve_static_spa() as base_url:
        page.goto(f"{base_url}/work-force")

        expect(page.get_by_role("heading", name="Work Force")).to_be_visible(timeout=30_000)
        expect(page.get_by_text("Employees").first).to_be_visible()
        expect(
            page.get_by_role("button", name=re.compile("Department Engineering"))
        ).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("Department Marketing"))).to_be_visible()
        expect(page.get_by_text("Backend Development Lead").first).to_be_visible()
        expect(page.get_by_text("System Agents").first).to_be_visible()
        expect(page.get_by_text("Workflows").first).to_be_visible()
        expect(page.get_by_text("Platform Developer").first).to_be_visible()
        expect(page.get_by_text("Hello World")).to_have_count(0)
        expect(page.get_by_text("Polly").first).to_be_visible()
        expect(page.get_by_text("Weekly Business Review").first).to_be_visible()
        expect(page.get_by_role("tab", name=re.compile("Overview"))).to_be_visible()
        expect(page.get_by_role("tab", name=re.compile("Config"))).to_be_visible()
        assert custom_agent_scan_requests == []
