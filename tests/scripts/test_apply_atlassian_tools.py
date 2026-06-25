from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "bytedesk"
    / "apply_atlassian_tools.py"
)
spec = importlib.util.spec_from_file_location("apply_atlassian_tools", SCRIPT_PATH)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_ensure_atlassian_tools_config_merges_builtins_and_prompt() -> None:
    updated, changed = module.ensure_atlassian_tools_config(
        {
            "name": "chief-of-staff",
            "prompt": "You are Maya.",
            "tools": {
                "builtins": ["web_search"],
                "agentic-inbox": {
                    "type": "mcp",
                    "url": "${AGENTIC_INBOX_MCP_URL}",
                },
            },
        }
    )

    assert changed is True
    assert updated["tools"]["builtins"] == [
        "web_search",
        "bytedesk_jira",
        "bytedesk_confluence",
    ]
    assert "agentic-inbox" in updated["tools"]
    assert "ATLASSIAN ACCESS (Jira / Confluence)" in updated["prompt"]


def test_ensure_atlassian_tools_config_accepts_existing_dict_builtin() -> None:
    updated, changed = module.ensure_atlassian_tools_config(
        {
            "prompt": "You are Maya.",
            "tools": {
                "builtins": [
                    {"name": "bytedesk_jira"},
                    "bytedesk_confluence",
                ],
            },
        }
    )

    assert changed is True
    assert updated["tools"]["builtins"] == [
        {"name": "bytedesk_jira"},
        "bytedesk_confluence",
    ]
    assert "ATLASSIAN ACCESS" in updated["prompt"]


def test_ensure_atlassian_tools_config_is_idempotent() -> None:
    updated, changed = module.ensure_atlassian_tools_config(
        {"prompt": "You are Maya."}
    )
    assert changed is True

    updated_again, changed_again = module.ensure_atlassian_tools_config(updated)

    assert changed_again is False
    assert updated_again == updated


def test_ensure_atlassian_tools_config_replaces_existing_note() -> None:
    updated, changed = module.ensure_atlassian_tools_config(
        {
            "prompt": (
                "You are Maya.\n\n"
                "ATLASSIAN ACCESS (Jira / Confluence)\n"
                "- Old note.\n"
            )
        }
    )

    assert changed is True
    assert updated["prompt"].count("ATLASSIAN ACCESS (Jira / Confluence)") == 1
    assert "Old note" not in updated["prompt"]
    assert "bytedesk_jira" in updated["prompt"]
