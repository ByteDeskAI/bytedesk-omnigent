from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "bytedesk" / "apply_github_mcp.py"
)
spec = importlib.util.spec_from_file_location("apply_github_mcp", SCRIPT_PATH)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_ensure_github_mcp_config_merges_tool_and_prompt() -> None:
    updated, changed = module.ensure_github_mcp_config(
        {
            "name": "platform-architect",
            "prompt": "You are Elias.",
            "tools": {
                "agentic-inbox": {
                    "type": "mcp",
                    "url": "${AGENTIC_INBOX_MCP_URL}",
                }
            },
        },
        mcp_url="${GITHUB_MCP_URL}",
    )

    assert changed is True
    assert "agentic-inbox" in updated["tools"]
    assert updated["tools"]["github"] == {
        "type": "mcp",
        "url": "${GITHUB_MCP_URL}",
        "tool_allowlist": module.ALLOWED_TOOLS,
    }
    assert "GITHUB ACCESS (github MCP)" in updated["prompt"]


def test_ensure_github_mcp_config_is_idempotent() -> None:
    updated, changed = module.ensure_github_mcp_config(
        {"prompt": "You are Elias."},
        mcp_url="${GITHUB_MCP_URL}",
    )
    assert changed is True

    updated_again, changed_again = module.ensure_github_mcp_config(
        updated,
        mcp_url="${GITHUB_MCP_URL}",
    )

    assert changed_again is False
    assert updated_again == updated


def test_ensure_github_mcp_config_replaces_existing_note() -> None:
    updated, changed = module.ensure_github_mcp_config(
        {
            "prompt": (
                "You are Elias.\n\n"
                "GITHUB ACCESS (github MCP)\n"
                "- Old note.\n"
            )
        },
        mcp_url="http://github-mcp/mcp",
    )

    assert changed is True
    assert updated["prompt"].count("GITHUB ACCESS (github MCP)") == 1
    assert "Old note" not in updated["prompt"]
    assert "repo as owner/name" in updated["prompt"]


def test_engineering_target_id_accepts_seed_id_and_persisted_display_name() -> None:
    assert module.engineering_target_id("platform-architect", "Anything") == "platform-architect"
    assert module.engineering_target_id("ag_123", "Elias Mercer") == "platform-architect"
    assert module.engineering_target_id("ag_123", "Maya Chen") is None
