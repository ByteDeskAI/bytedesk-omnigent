from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "bytedesk" / "apply_goal_commander.py"
)
spec = importlib.util.spec_from_file_location("apply_goal_commander", SCRIPT_PATH)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_ensure_commander_config_merges_full_toolset_params_and_prompt() -> None:
    updated, changed = module.ensure_goal_commander_config(
        {
            "name": "goal-commander",
            "prompt": "You operate the org.",
            "tools": {"builtins": ["web_search"]},
        }
    )
    assert changed is True
    assert updated["params"]["goalCommander"] is True
    assert updated["executor"]["config"]["harness"] == "claude-sdk"
    builtins = updated["tools"]["builtins"]
    for tool in (
        "goal_set_posture", "goal_read_frontier", "goal_decompose",
        "goal_batch_approve", "goal_adjust_budget", "goal_prioritize",
        "goal_read_decisions", "goal_read_ledger",
    ):
        assert tool in builtins
    assert builtins[0] == "web_search"  # existing builtins preserved
    assert "GOALS COMMAND CENTER" in updated["prompt"]
    assert "kill switch" in updated["prompt"]


def test_ensure_commander_config_is_idempotent() -> None:
    updated, changed = module.ensure_goal_commander_config({"prompt": "Operator."})
    assert changed is True
    again, changed_again = module.ensure_goal_commander_config(updated)
    assert changed_again is False
    assert again == updated


def test_ensure_commander_config_replaces_existing_note() -> None:
    updated, changed = module.ensure_goal_commander_config(
        {"prompt": "Operator.\n\nGOALS COMMAND CENTER\n- Old note.\n"}
    )
    assert changed is True
    assert updated["prompt"].count("GOALS COMMAND CENTER") == 1
    assert "Old note" not in updated["prompt"]
    assert "goal_set_posture" in updated["prompt"]
