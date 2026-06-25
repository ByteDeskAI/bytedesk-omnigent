from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "bytedesk" / "apply_agentic_inbox.py"
)
spec = importlib.util.spec_from_file_location("apply_agentic_inbox", SCRIPT_PATH)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_display_name_to_email_uses_first_and_last_tokens() -> None:
    assert (
        module.display_name_to_email("Seo / GEO Growth Lead", "agents.dev.bytedesk.ai")
        == "seo.lead@agents.dev.bytedesk.ai"
    )


def test_persona_agents_excludes_workflows_and_agents_without_display_name() -> None:
    payload = {
        "agents": [
            {"id": "ag_1", "display_name": "Maya Rivera"},
            {"id": "ag_2", "display_name": "Weekly Review", "workflow": True},
            {"id": "ag_3", "display_name": "Pipeline", "params": {"workflow": True}},
            {"id": "ag_4", "name": "raw-template"},
        ]
    }

    assert module.persona_agents(payload) == [{"id": "ag_1", "display_name": "Maya Rivera"}]


def test_ensure_agentic_inbox_config_merges_tool_params_and_prompt() -> None:
    config = {
        "name": "chief-of-staff",
        "prompt": "You are Maya.",
        "params": {"displayName": "Maya Rivera"},
        "tools": {
            "bytedesk-platform": {
                "type": "mcp",
                "url": "${BYTEDESK_PLATFORM_MCP_URL}",
            }
        },
    }

    updated, changed = module.ensure_agentic_inbox_config(
        config,
        display_name="Maya Rivera",
        email="maya.rivera@agents.dev.bytedesk.ai",
        mcp_url="https://inbox.agents.dev.bytedesk.ai/mcp",
    )

    assert changed is True
    assert updated["params"]["email"] == "maya.rivera@agents.dev.bytedesk.ai"
    assert updated["params"]["mailboxId"] == "maya.rivera@agents.dev.bytedesk.ai"
    assert "bytedesk-platform" in updated["tools"]
    assert updated["tools"]["agentic-inbox"] == {
        "type": "mcp",
        "url": "https://inbox.agents.dev.bytedesk.ai/mcp",
        "headers": {
            "CF-Access-Client-Id": "${AGENTIC_INBOX_CF_ACCESS_CLIENT_ID}",
            "CF-Access-Client-Secret": "${AGENTIC_INBOX_CF_ACCESS_CLIENT_SECRET}",
        },
        "tool_allowlist": module.ALLOWED_TOOLS,
    }
    assert "EMAIL ACCOUNT (agentic-inbox)" in updated["prompt"]
    assert "mailboxId to maya.rivera@agents.dev.bytedesk.ai" in updated["prompt"]


def test_ensure_agentic_inbox_config_is_idempotent() -> None:
    updated, changed = module.ensure_agentic_inbox_config(
        {
            "prompt": "You are Maya.",
            "params": {"displayName": "Maya Rivera"},
        },
        display_name="Maya Rivera",
        email="maya.rivera@agents.dev.bytedesk.ai",
        mcp_url="https://inbox.agents.dev.bytedesk.ai/mcp",
    )
    assert changed is True

    updated_again, changed_again = module.ensure_agentic_inbox_config(
        updated,
        display_name="Maya Rivera",
        email="maya.rivera@agents.dev.bytedesk.ai",
        mcp_url="https://inbox.agents.dev.bytedesk.ai/mcp",
    )

    assert changed_again is False
    assert updated_again == updated


def test_ensure_agentic_inbox_config_replaces_existing_email_note() -> None:
    updated, changed = module.ensure_agentic_inbox_config(
        {
            "prompt": (
                "You are Maya.\n\n"
                "EMAIL ACCOUNT (agentic-inbox)\n"
                "- Your personal email address is maya.rivera@agents.dev.bytedesk.ai.\n"
            ),
            "params": {"displayName": "Maya Rivera"},
        },
        display_name="Maya Rivera",
        email="maya.rivera@agents.bytedesk.ai",
        mcp_url="https://inbox.agents.bytedesk.ai/mcp",
    )

    assert changed is True
    assert updated["prompt"].count("EMAIL ACCOUNT (agentic-inbox)") == 1
    assert "maya.rivera@agents.bytedesk.ai" in updated["prompt"]
    assert "maya.rivera@agents.dev.bytedesk.ai" not in updated["prompt"]
