"""UI smoke for the Work Force admin route."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


@dataclass
class _WorkforceRouteState:
    custom_agent_scan_requests: list[str]
    tool_mutations: list[dict[str, object]]
    override_mutations: list[dict[str, object]]


_TOOL_CATALOG = [
    {
        "toolKey": "web_search",
        "label": "Web search",
        "description": "Search the web.",
        "group": "Web",
        "mechanism": "builtin",
    },
    {
        "toolKey": "sys_os_write",
        "label": "Write files",
        "description": "Write local files.",
        "group": "Local OS",
        "mechanism": "os_env",
    },
]


def _tool_assignment(
    scope_kind: str,
    scope_id: str,
    tool_key: str,
    enabled: bool,
) -> dict[str, object]:
    return {
        "id": f"wftool_{scope_kind}_{scope_id}_{tool_key}",
        "scopeKind": scope_kind,
        "scopeId": scope_id,
        "toolKey": tool_key,
        "itemKey": tool_key,
        "enabled": enabled,
        "createdAt": 1,
        "updatedAt": 2,
        "version": 1,
        "metadata": {},
    }


def _agent_override(tool_key: str, enabled: bool) -> dict[str, object]:
    return {
        "id": f"wfoverride_ag_employee_tool_{tool_key}",
        "agentId": "ag_employee",
        "itemKind": "tool",
        "itemKey": tool_key,
        "enabled": enabled,
        "createdAt": 1,
        "updatedAt": 2,
        "version": 1,
        "metadata": {},
    }


def _register_api_routes(page: Page) -> _WorkforceRouteState:
    """Stub the server API calls the Work Force route needs for hydration."""
    custom_agent_scan_requests: list[str] = []
    tool_mutations: list[dict[str, object]] = []
    override_mutations: list[dict[str, object]] = []
    scope_tools: dict[tuple[str, str], dict[str, bool]] = {
        ("organization", "organization"): {},
        ("department", "engineering"): {},
    }
    agent_tool_overrides: dict[str, bool] = {}

    def scope_detail(scope_kind: str, scope_id: str) -> dict[str, object]:
        tools = [
            _tool_assignment(scope_kind, scope_id, tool_key, enabled)
            for tool_key, enabled in sorted(scope_tools.get((scope_kind, scope_id), {}).items())
        ]
        return {
            "scopeKind": scope_kind,
            "scopeId": scope_id,
            "instruction": None,
            "connectors": [],
            "skills": [],
            "tools": tools,
            "revision": 7 + len(tool_mutations) + len(override_mutations),
        }

    def effective_tools() -> list[dict[str, object]]:
        catalog_by_key = {str(item["toolKey"]): item for item in _TOOL_CATALOG}
        tool_keys = {
            *scope_tools[("organization", "organization")].keys(),
            *scope_tools[("department", "engineering")].keys(),
            *agent_tool_overrides.keys(),
        }
        rows: list[dict[str, object]] = []
        for tool_key in sorted(tool_keys):
            inherited_from = []
            for scope_kind, scope_id in (
                ("organization", "organization"),
                ("department", "engineering"),
            ):
                enabled = scope_tools[(scope_kind, scope_id)].get(tool_key)
                if enabled is not None:
                    inherited_from.append(
                        _tool_assignment(scope_kind, scope_id, tool_key, enabled)
                    )
            override_enabled = agent_tool_overrides.get(tool_key)
            inherited_enabled = (
                bool(inherited_from[-1]["enabled"]) if inherited_from else False
            )
            enabled = override_enabled if override_enabled is not None else inherited_enabled
            override = (
                _agent_override(tool_key, override_enabled)
                if override_enabled is not None
                else None
            )
            catalog_item = catalog_by_key.get(tool_key, {})
            rows.append(
                {
                    "itemKey": tool_key,
                    "toolKey": tool_key,
                    "label": catalog_item.get("label", tool_key),
                    "description": catalog_item.get("description", ""),
                    "group": catalog_item.get("group", "Built-in"),
                    "mechanism": catalog_item.get("mechanism", "managed"),
                    "enabled": enabled,
                    "inherited": bool(inherited_from),
                    "inheritedFrom": inherited_from,
                    "override": override,
                }
            )
        return rows

    def effective_agent() -> dict[str, object]:
        overrides = [
            _agent_override(tool_key, enabled)
            for tool_key, enabled in sorted(agent_tool_overrides.items())
        ]
        return {
            "agentId": "ag_employee",
            "found": True,
            "category": "employee",
            "department": "Engineering",
            "departmentSlug": "engineering",
            "revision": 7 + len(tool_mutations) + len(override_mutations),
            "instructions": [],
            "connectors": [],
            "skills": [],
            "tools": effective_tools(),
            "overrides": overrides,
            "materializations": [],
        }

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
        if path == "/v1/connectors/catalog":
            fulfill_json(route, {"data": []})
            return
        if path == "/v1/connectors/agent-grants":
            fulfill_json(route, {"data": []})
            return
        if path == "/v1/skills/installed":
            fulfill_json(route, {"data": []})
            return
        if path == "/v1/workforce/scopes":
            fulfill_json(
                route,
                {
                    "scopes": [
                        {
                            "scopeKind": "organization",
                            "scopeId": "organization",
                            "label": "Organization",
                            "agentIds": ["ag_employee"],
                        },
                        {
                            "scopeKind": "department",
                            "scopeId": "engineering",
                            "label": "Engineering",
                            "agentIds": ["ag_employee"],
                        },
                    ],
                    "revision": 7 + len(tool_mutations) + len(override_mutations),
                },
            )
            return
        if path == "/v1/workforce/tools/catalog":
            fulfill_json(route, {"tools": _TOOL_CATALOG})
            return
        if path == "/v1/workforce/scopes/organization":
            fulfill_json(route, scope_detail("organization", "organization"))
            return
        if path == "/v1/workforce/scopes/department/engineering":
            fulfill_json(route, scope_detail("department", "engineering"))
            return
        if path in {
            "/v1/workforce/scopes/organization/tools",
            "/v1/workforce/scopes/department/engineering/tools",
        }:
            body = request.post_data_json
            scope_kind = "organization" if "/organization/" in path else "department"
            scope_id = "organization" if scope_kind == "organization" else "engineering"
            tool_key = str(body["toolKey"])
            enabled = bool(body["enabled"])
            scope_tools[(scope_kind, scope_id)][tool_key] = enabled
            tool_mutations.append(
                {
                    "scopeKind": scope_kind,
                    "scopeId": scope_id,
                    "toolKey": tool_key,
                    "enabled": enabled,
                }
            )
            assignment = _tool_assignment(scope_kind, scope_id, tool_key, enabled)
            fulfill_json(
                route,
                {
                    "assignment": assignment,
                    "reconciledAgentIds": ["ag_employee"],
                    "scope": scope_detail(scope_kind, scope_id),
                },
            )
            return
        if path == "/v1/workforce/agents/ag_employee/effective":
            fulfill_json(route, effective_agent())
            return
        if path == "/v1/workforce/agents/ag_employee/overrides":
            body = request.post_data_json
            assert body["itemKind"] == "tool"
            tool_key = str(body["itemKey"])
            enabled = bool(body["enabled"])
            agent_tool_overrides[tool_key] = enabled
            override_mutations.append(
                {
                    "agentId": "ag_employee",
                    "itemKind": "tool",
                    "itemKey": tool_key,
                    "enabled": enabled,
                }
            )
            override = _agent_override(tool_key, enabled)
            fulfill_json(route, {"override": override, "effective": effective_agent()})
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
                            "id": "ag_harness",
                            "name": "claude-native-ui",
                            "display_name": "Claude Code",
                            "description": "Native Claude launcher.",
                            "harness": "claude-native",
                            "skills": [],
                            "department": None,
                            "title": None,
                            "workflow": False,
                            "category": "harness",
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
    return _WorkforceRouteState(
        custom_agent_scan_requests=custom_agent_scan_requests,
        tool_mutations=tool_mutations,
        override_mutations=override_mutations,
    )


def test_work_force_page_renders_agent_admin_shell(page: Page, built_spa: None) -> None:
    """The Work Force route shows agent sections and editor tabs."""
    del built_spa

    route_state = _register_api_routes(page)
    with _serve_static_spa() as base_url:
        page.goto(f"{base_url}/work-force")

        expect(page.get_by_role("heading", name="Work Force")).to_be_visible(timeout=30_000)
        expect(page.get_by_text("Employees").first).to_be_visible()
        engineering = page.get_by_role("button", name=re.compile("Department Engineering"))
        expect(engineering).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("Department Marketing"))).to_be_visible()
        expect(page.get_by_text("Backend Development Lead").first).to_be_visible()
        expect(page.get_by_text("System Agents").first).to_be_visible()
        expect(page.get_by_text("Harnesses").first).to_be_visible()
        expect(page.get_by_text("Workflows").first).to_be_visible()
        if engineering.get_attribute("aria-expanded") != "true":
            engineering.click()
        expect(page.get_by_text("Platform Developer").first).to_be_visible()
        expect(page.get_by_text("Hello World")).to_have_count(0)
        for section_name in ("Harnesses", "System Agents", "Workflows"):
            trigger = page.get_by_role("button", name=section_name, exact=True)
            if trigger.get_attribute("aria-expanded") != "true":
                trigger.click()
        expect(page.get_by_text("Claude Code").first).to_be_visible()
        expect(page.get_by_text("Polly").first).to_be_visible()
        expect(page.get_by_text("Weekly Business Review").first).to_be_visible()
        expect(page.get_by_role("tab", name=re.compile("Overview"))).to_be_visible()
        expect(page.get_by_role("tab", name=re.compile("Config"))).to_be_visible()
        assert route_state.custom_agent_scan_requests == []


def test_work_force_builtin_tool_permissions_toggle_at_each_level(
    page: Page,
    built_spa: None,
) -> None:
    """Org, department, and agent builtin-tool toggles update the effective UI state."""
    del built_spa

    route_state = _register_api_routes(page)
    with _serve_static_spa() as base_url:
        page.goto(f"{base_url}/work-force")

        expect(page.get_by_role("heading", name="Work Force")).to_be_visible(timeout=30_000)
        engineering = page.get_by_role("button", name=re.compile("Department Engineering"))
        expect(engineering).to_be_visible()
        if engineering.get_attribute("aria-expanded") != "true":
            engineering.click()
        page.get_by_text("Platform Developer").first.click()
        expect(page.get_by_role("heading", name="Platform Developer")).to_be_visible()
        page.get_by_role("tab", name=re.compile("Permissions")).click()

        scope_row = page.get_by_test_id("scope-tool-row-web_search")
        effective_row = page.get_by_test_id("effective-tool-row-web_search")

        page.get_by_role("button", name="Organization").click()
        expect(page.get_by_text("Organization Builtin Tools")).to_be_visible()
        scope_row.get_by_role("button", name="Grant here").click()
        expect(scope_row.get_by_text("Granted here")).to_be_visible(timeout=15_000)
        expect(effective_row.get_by_text("Enabled")).to_be_visible(timeout=15_000)

        scope_row.get_by_role("button", name="Deny here").click()
        expect(scope_row.get_by_text("Denied here")).to_be_visible(timeout=15_000)
        expect(effective_row.get_by_text("Disabled")).to_be_visible(timeout=15_000)

        page.get_by_role("button", name="Engineering", exact=True).click()
        expect(page.get_by_text("Engineering Builtin Tools")).to_be_visible()
        scope_row.get_by_role("button", name="Grant here").click()
        expect(scope_row.get_by_text("Granted here")).to_be_visible(timeout=15_000)
        expect(effective_row.get_by_text("Enabled")).to_be_visible(timeout=15_000)

        scope_row.get_by_role("button", name="Deny here").click()
        expect(scope_row.get_by_text("Denied here")).to_be_visible(timeout=15_000)
        expect(effective_row.get_by_text("Disabled")).to_be_visible(timeout=15_000)

        effective_row.get_by_role("button", name="Enable for agent").click()
        expect(effective_row.get_by_text("Enabled")).to_be_visible(timeout=15_000)
        expect(effective_row.get_by_role("button", name="Disable for agent")).to_be_visible(
            timeout=15_000
        )

        effective_row.get_by_role("button", name="Disable for agent").click()
        expect(effective_row.get_by_text("Disabled")).to_be_visible(timeout=15_000)
        expect(effective_row.get_by_role("button", name="Enable for agent")).to_be_visible(
            timeout=15_000
        )

    assert route_state.custom_agent_scan_requests == []
    assert route_state.tool_mutations == [
        {
            "scopeKind": "organization",
            "scopeId": "organization",
            "toolKey": "web_search",
            "enabled": True,
        },
        {
            "scopeKind": "organization",
            "scopeId": "organization",
            "toolKey": "web_search",
            "enabled": False,
        },
        {
            "scopeKind": "department",
            "scopeId": "engineering",
            "toolKey": "web_search",
            "enabled": True,
        },
        {
            "scopeKind": "department",
            "scopeId": "engineering",
            "toolKey": "web_search",
            "enabled": False,
        },
    ]
    assert route_state.override_mutations == [
        {
            "agentId": "ag_employee",
            "itemKind": "tool",
            "itemKey": "web_search",
            "enabled": True,
        },
        {
            "agentId": "ag_employee",
            "itemKind": "tool",
            "itemKey": "web_search",
            "enabled": False,
        },
    ]
