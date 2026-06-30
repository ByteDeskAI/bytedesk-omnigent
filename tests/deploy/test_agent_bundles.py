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


def test_nested_platform_developer_child_removed() -> None:
    assert not (_AGENTS / "chief-of-staff" / "agents").exists()
