#!/usr/bin/env python3
"""Provision the Goals Command Center operator agent (BDP-2598, ADR command-center).

A chief-of-staff-class ``goal-commander`` agent that holds the FULL goal-engine
toolset and runs the command center: set, monitor, observe, and drive goals on the
founder's behalf. Mirrors ``apply_goal_planner.py`` — it updates the agent through
Omnigent's ``/v1/agents/{agent_id}/image`` API and is idempotent (re-running makes
no change once applied).

The default target is the dedicated ``goal-commander`` agent. Pass ``--agent-id``
to point an existing agent (e.g. ``chief-of-staff``) at the commander capability.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from apply_agentic_inbox import (  # noqa: E402
    ApiError,
    _json_request,
    get_agent_image,
    login,
    put_agent_image,
)

DEFAULT_BASE_URL = "http://omnigent.bytedesk.localhost"
DEFAULT_AGENT_NAMES = ("goal-commander",)
GOAL_COMMANDER_MARKER = "GOALS COMMAND CENTER"

# The FULL engine-driving toolset: existing create/list/claim/advance/dependency,
# the admin CRUD reads, plus the Wave-5 commander tools.
GOAL_COMMANDER_BUILTINS = [
    "bytedesk_jira",
    "bytedesk_confluence",
    "goal_create",
    "goal_list",
    "goal_claim",
    "goal_advance",
    "goal_dependency_update",
    "goal_prioritize",
    "goal_adjust_budget",
    "goal_set_posture",
    "goal_read_frontier",
    "goal_read_decisions",
    "goal_read_ledger",
    "goal_batch_approve",
    "goal_decompose",
]


def goal_commander_note() -> str:
    return (
        f"{GOAL_COMMANDER_MARKER}\n"
        "- You are the Goals Command Center operator: set, monitor, observe, and "
        "drive goals on the founder's behalf. The founder is the board (direction, "
        "budget, the switch); you are the operator.\n"
        "- SET: goal_create (use proposed=true to park a discovered opportunity for "
        "approval), goal_decompose to split a goal into a child tree, goal_prioritize "
        "to reorder, goal_batch_approve to activate proposed/draft goals.\n"
        "- FUND: goal_adjust_budget sets a scope's cap/limits; check headroom against "
        "the frontier before raising a cap.\n"
        "- OBSERVE: goal_read_frontier (what's running + why, ranked by ROI with "
        "waiting_reasons), goal_read_decisions (fund/skip replay), goal_read_ledger "
        "(realized value). Read before you act.\n"
        "- ARM: goal_set_posture flips autonomy. 'gated' (the kill switch) is always "
        "available and reachable in one step; 'full_auto' is governance-gated — if it "
        "is refused, surface that to the founder, do not retry past the gate.\n"
        "- You are a client of the engine, never a second source of truth: every "
        "mutation goes through these tools, governed and audited."
    )


def merge_goal_commander_note(prompt: str, note: str) -> tuple[str, bool]:
    if GOAL_COMMANDER_MARKER not in prompt:
        return f"{prompt.rstrip()}\n\n{note}\n", True
    before, marker, _after = prompt.partition(GOAL_COMMANDER_MARKER)
    merged = f"{before.rstrip()}\n\n{marker}{note.removeprefix(GOAL_COMMANDER_MARKER)}\n"
    return merged, merged != prompt


def _builtin_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
        return entry["name"]
    return None


def ensure_goal_commander_config(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Idempotently merge the commander executor/params/toolset/prompt into ``config``."""
    updated = copy.deepcopy(config)
    changed = False

    executor = updated.get("executor")
    if executor is None:
        executor = {}
    if not isinstance(executor, dict):
        raise ValueError("config.executor must be a mapping when present")
    executor_config = executor.get("config")
    if executor_config is None:
        executor_config = {}
    if not isinstance(executor_config, dict):
        raise ValueError("config.executor.config must be a mapping when present")
    if executor.get("type") != "omnigent":
        executor["type"] = "omnigent"
        changed = True
    if executor_config.get("harness") != "claude-sdk":
        executor_config["harness"] = "claude-sdk"
        changed = True
    if executor.get("config") != executor_config:
        executor["config"] = executor_config
        changed = True
    if updated.get("executor") != executor:
        updated["executor"] = executor
        changed = True

    params = updated.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("config.params must be a mapping when present")
    if params.get("goalCommander") is not True:
        params["goalCommander"] = True
        updated["params"] = params
        changed = True

    tools = updated.get("tools")
    if tools is None:
        tools = {}
    if not isinstance(tools, dict):
        raise ValueError("config.tools must be a mapping when present")
    builtins = tools.get("builtins")
    if builtins is None:
        builtins = []
    if not isinstance(builtins, list):
        raise ValueError("config.tools.builtins must be a list when present")
    builtin_names = {_builtin_name(entry) for entry in builtins}
    next_builtins = list(builtins)
    for builtin in GOAL_COMMANDER_BUILTINS:
        if builtin not in builtin_names:
            next_builtins.append(builtin)
            changed = True
    if tools.get("builtins") != next_builtins:
        tools["builtins"] = next_builtins
        updated["tools"] = tools
        changed = True

    note = goal_commander_note()
    prompt = updated.get("prompt")
    if isinstance(prompt, str):
        next_prompt, prompt_changed = merge_goal_commander_note(prompt, note)
        if prompt_changed:
            updated["prompt"] = next_prompt
            changed = True
    elif prompt is None:
        updated["prompt"] = f"{note}\n"
        changed = True
    else:
        raise ValueError("config.prompt must be a string when present")

    return updated, changed


def _agents_from_response(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        agents = payload.get("agents") or payload.get("items") or payload.get("data") or []
    else:
        agents = payload
    if not isinstance(agents, list):
        raise ValueError("agents response did not contain a list")
    return [agent for agent in agents if isinstance(agent, dict)]


def list_all_agents(base_url: str, token: str) -> list[dict[str, Any]]:
    response = _json_request(
        "GET", f"{base_url.rstrip('/')}/v1/agents?limit=1000&order=asc", token=token
    )
    return _agents_from_response(response.body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.getenv("OMNIGENT_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username", default=os.getenv("OMNIGENT_USERNAME"))
    parser.add_argument("--password", default=os.getenv("OMNIGENT_PASSWORD"))
    parser.add_argument("--token", default=os.getenv("OMNIGENT_BEARER_TOKEN"))
    parser.add_argument(
        "--agent-id",
        action="append",
        default=[],
        help="Only update this agent id; repeatable. Defaults to the goal-commander agent.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token
    if not token:
        if not args.username or not args.password:
            print(
                "Provide OMNIGENT_BEARER_TOKEN or OMNIGENT_USERNAME/OMNIGENT_PASSWORD.",
                file=sys.stderr,
            )
            return 2
        token = login(args.server, args.username, args.password)

    agents = list_all_agents(args.server, token)
    explicit_ids = set(args.agent_id)
    default_names = set(DEFAULT_AGENT_NAMES)
    found: set[str] = set()
    updated_count = unchanged_count = skipped_count = 0

    for agent in agents:
        agent_id = agent.get("id")
        agent_name = agent.get("name")
        display_name = agent.get("display_name") or agent_name or ""
        if not isinstance(agent_id, str) or not isinstance(agent_name, str):
            skipped_count += 1
            continue
        selected = agent_id in explicit_ids or (not explicit_ids and agent_name in default_names)
        if not selected:
            skipped_count += 1
            continue
        found.add(agent_id if explicit_ids else agent_name)

        image, etag = get_agent_image(args.server, token, agent_id)
        config = image.get("config")
        if not isinstance(config, dict):
            raise ApiError(f"{agent_id}: image config was not an object")

        next_config, changed = ensure_goal_commander_config(config)
        if not changed:
            unchanged_count += 1
            print(f"unchanged {agent_id} {display_name}".rstrip())
            continue
        if args.dry_run:
            updated_count += 1
            print(f"would-update {agent_id} {display_name}".rstrip())
            continue
        put_agent_image(args.server, token, agent_id, next_config, etag)
        updated_count += 1
        print(f"updated {agent_id} {display_name}".rstrip())

    expected = explicit_ids if explicit_ids else default_names
    missing = sorted(expected - found)
    print(
        json.dumps(
            {
                "updated": updated_count,
                "unchanged": unchanged_count,
                "skipped": skipped_count,
                "missing": missing,
                "dry_run": args.dry_run,
            },
            sort_keys=True,
        )
    )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
