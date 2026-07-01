"""Validity + structure tests for the ByteDesk agent bundles (BDP-2148, ADR-0133).

Parsing each bundle via the omnigent spec loader transitively validates the
guardrail policies (an unregistered/misshaped handler is rejected at parse), so
this catches the FU2 ``allowed_subagents`` wiring. YAML-level assertions pin the
FU2 structural changes without depending on AgentSpec internals. Runtime
registration + launch-by-id are proven in the in-cluster phase.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bytedesk_omnigent.connectors.manifests import google_workspace_connector_manifest
from omnigent.spec import parse

_AGENTS = Path(__file__).resolve().parents[2] / "deploy" / "bytedesk" / "agents"

MAYA_ALLOWED_SUBAGENTS = [
    "client-research-lead",
    "customer-success-and-support-lead",
    "hr-org-designer",
    "marketing-director",
    "platform-architect",
    "platform-developer",
    "product-ops-director",
    "sales-enablement-lead",
]

def _yaml(name: str) -> dict:
    return yaml.safe_load((_AGENTS / name / "config.yaml").read_text())


def _builtin_names(cfg: dict) -> set[str]:
    names: set[str] = set()
    for item in (cfg.get("tools") or {}).get("builtins") or []:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
    return names


def test_all_top_level_bundles_parse() -> None:
    """Every top-level bundle parses (validates registered policy handlers)."""
    names = sorted(p.parent.name for p in _AGENTS.glob("*/config.yaml"))
    assert {"chief-of-staff", "platform-developer"} <= set(names), names
    for name in names:
        parse(_AGENTS / name, expand_env=False)


def test_platform_developer_is_standalone_with_manager_edge() -> None:
    cfg = _yaml("platform-developer")
    assert cfg["name"] == "platform-developer"
    managers = cfg.get("params", {}).get("managers") or []
    assert any(m.get("id") == "chief-of-staff" for m in managers), managers


def test_maya_has_allowed_subagents_policy_scoped_to_platform_developer() -> None:
    cfg = _yaml("chief-of-staff")
    policies = cfg["guardrails"]["policies"]
    assert "allowed_subagents" in policies
    pol = policies["allowed_subagents"]
    assert pol["function"]["path"] == "omnigent.inner.nessie.policies.allowed_subagents"
    assert pol["function"]["arguments"]["allowed_agents"] == MAYA_ALLOWED_SUBAGENTS


def test_maya_platform_mcp_allowlist_leaves_google_workspace_to_connectors() -> None:
    cfg = _yaml("chief-of-staff")
    allowlist = cfg["tools"]["bytedesk-platform"]["tool_allowlist"]
    connector_tools = {
        tool.mcp_tool
        for service in google_workspace_connector_manifest().services
        for tool in service.tools
    }

    assert not any(tool.startswith("googleworkspace_") for tool in allowlist)
    assert "drive_search" in connector_tools


def test_maya_no_longer_declares_a_static_child() -> None:
    cfg = _yaml("chief-of-staff")
    tools = cfg.get("tools") or {}  # comment-only `tools:` parses to None
    assert not tools.get("agents")


def test_brand_creative_agent_can_generate_images() -> None:
    cfg = _yaml("brand-and-creative-director")
    builtins = cfg["tools"]["builtins"]
    assert {"name": "bytedesk_generate_image"} in builtins
    assert "bytedesk_generate_image" in cfg["prompt"]


def test_web_design_agent_can_generate_website_assets() -> None:
    cfg = _yaml("web-design-director")
    assert "bytedesk_generate_image" in _builtin_names(cfg)
    assert "file_id" in cfg["prompt"]


def test_web_development_agent_can_package_and_drive_handoff() -> None:
    cfg = _yaml("web-development-lead")
    assert {
        "list_files",
        "download_file",
        "bytedesk_package_website_zip",
    } <= _builtin_names(cfg)
    assert "google_workspace_drive_file_upload_session" in cfg["prompt"]
    assert "session_id" in cfg["prompt"]
    assert "client_drive_folder_id" in cfg["prompt"]


def test_website_design_to_zip_workflow_delegates_design_and_development() -> None:
    cfg = _yaml("website-design-to-zip-factory")
    assert str(cfg["params"]["workflow"]).lower() == "true"
    assert cfg["params"]["orchestrator"] == "product-ops-director"
    allowed = cfg["guardrails"]["policies"]["allowed_subagents"]["function"]["arguments"][
        "allowed_agents"
    ]
    assert allowed == ["web-design-director", "web-development-lead"]
    assert "bytedesk_package_website_zip" in cfg["prompt"]
    assert "google_workspace_drive_file_upload_session" in cfg["prompt"]
    assert "session_id" in cfg["prompt"]


def test_website_blueprint_factory_has_strict_review_loops() -> None:
    cfg = _yaml("website-design-to-zip-blueprint-factory")

    assert cfg["executor"]["type"] == "blueprint"
    assert str(cfg["params"]["workflow"]).lower() == "true"
    assert cfg["params"]["orchestrator"] == "product-ops-director"

    nodes = {node["id"]: node for node in cfg["blueprint"]["nodes"]}
    assert {
        "normalize_request",
        "generate_designs",
        "design_feedback_loop",
        "build_html",
        "html_feedback_loop",
        "package_zip",
        "upload_to_drive",
        "delivery_feedback_loop",
        "final_output",
    } <= set(nodes)

    assert str(nodes["normalize_request"]["metadata"]["expect_json"]).lower() == "true"
    assert nodes["generate_designs"]["target"] == "web-design-director"
    assert nodes["build_html"]["target"] == "web-development-lead"
    assert nodes["upload_to_drive"]["input"]["folder_rule"].startswith(
        "Prefer client_website_folder_id"
    )

    design_loop = nodes["design_feedback_loop"]["loop"]
    html_loop = nodes["html_feedback_loop"]["loop"]
    delivery_loop = nodes["delivery_feedback_loop"]["loop"]
    assert design_loop["max_iterations"] == 3
    assert html_loop["max_iterations"] == 3
    assert delivery_loop["max_iterations"] == 3
    assert design_loop["until"]["path"] == "$.nodes.review_designs.output.approved"
    assert html_loop["until"]["path"] == "$.nodes.review_html.output.approved"
    assert delivery_loop["until"]["path"] == "$.nodes.review_delivery.output.approved"

    loop_nodes = {
        child["id"]: child
        for loop in [design_loop, html_loop, delivery_loop]
        for child in loop["body"]
    }
    assert loop_nodes["review_designs"]["target"] == "website-design-quality-gate"
    assert loop_nodes["review_html"]["target"] == "website-html-quality-gate"
    assert loop_nodes["review_delivery"]["target"] == "website-delivery-quality-gate"
    assert all(
        str(child["metadata"]["expect_json"]).lower() == "true"
        for child in loop_nodes.values()
    )


def test_nested_platform_developer_child_removed() -> None:
    assert not (_AGENTS / "chief-of-staff" / "agents").exists()
