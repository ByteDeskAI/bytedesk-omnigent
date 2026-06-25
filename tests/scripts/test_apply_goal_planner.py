from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "bytedesk"
    / "apply_goal_planner.py"
)
spec = importlib.util.spec_from_file_location("apply_goal_planner", SCRIPT_PATH)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_ensure_goal_planner_config_merges_harness_tools_params_and_prompt() -> None:
    updated, changed = module.ensure_goal_planner_config(
        {
            "name": "chief-of-staff",
            "prompt": "You are Maya.",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
            "params": {"department": "Operations"},
            "tools": {"builtins": ["web_search"]},
        }
    )

    assert changed is True
    assert updated["executor"]["config"]["harness"] == "claude-sdk"
    assert updated["params"]["goalPlanner"] is True
    assert updated["tools"]["builtins"] == [
        "web_search",
        "bytedesk_jira",
        "bytedesk_confluence",
        "goal_list",
        "goal_create",
        "goal_dependency_update",
    ]
    assert "GOAL PLANNING INTERVIEW" in updated["prompt"]
    assert "AskUserQuestion" in updated["prompt"]


def test_ensure_goal_planner_config_accepts_existing_dict_builtins() -> None:
    updated, changed = module.ensure_goal_planner_config(
        {
            "prompt": "Planner.",
            "tools": {
                "builtins": [
                    {"name": "bytedesk_jira"},
                    "bytedesk_confluence",
                    "goal_list",
                    "goal_create",
                    "goal_dependency_update",
                ]
            },
        }
    )

    assert changed is True
    assert updated["tools"]["builtins"] == [
        {"name": "bytedesk_jira"},
        "bytedesk_confluence",
        "goal_list",
        "goal_create",
        "goal_dependency_update",
    ]


def test_ensure_goal_planner_config_is_idempotent() -> None:
    updated, changed = module.ensure_goal_planner_config({"prompt": "Planner."})
    assert changed is True

    updated_again, changed_again = module.ensure_goal_planner_config(updated)

    assert changed_again is False
    assert updated_again == updated


def test_ensure_goal_planner_config_replaces_existing_note() -> None:
    updated, changed = module.ensure_goal_planner_config(
        {
            "prompt": (
                "You are Maya.\n\n"
                "GOAL PLANNING INTERVIEW\n"
                "- Old note.\n"
            )
        }
    )

    assert changed is True
    assert updated["prompt"].count("GOAL PLANNING INTERVIEW") == 1
    assert "Old note" not in updated["prompt"]
    assert "goal_create" in updated["prompt"]
