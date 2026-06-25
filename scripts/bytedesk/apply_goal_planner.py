#!/usr/bin/env python3
"""Persist the goal-planning interview capability into planner agent images.

This updates agents through Omnigent's ``/v1/agents/{agent_id}/image`` API,
not by hand-editing saved images. The default target set is the dedicated
``goal-planner`` agent when present, plus Maya (``chief-of-staff``) as the
current fallback planner persona.
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
DEFAULT_AGENT_NAMES = ("goal-planner", "chief-of-staff")
GOAL_PLANNER_MARKER = "GOAL PLANNING INTERVIEW"
GOAL_PLANNER_BUILTINS = [
    "bytedesk_jira",
    "bytedesk_confluence",
    "goal_list",
    "goal_create",
    "goal_dependency_update",
]


def _agents_from_response(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        agents = payload.get("agents") or payload.get("items") or payload.get("data") or []
    else:
        agents = payload
    if not isinstance(agents, list):
        raise ValueError("agents response did not contain a list")
    return [agent for agent in agents if isinstance(agent, dict)]


def _is_workflow(agent: dict[str, Any]) -> bool:
    if agent.get("workflow") is True:
        return True
    params = agent.get("params")
    return isinstance(params, dict) and params.get("workflow") is True


def list_all_agents(base_url: str, token: str) -> list[dict[str, Any]]:
    response = _json_request(
        "GET",
        f"{base_url.rstrip('/')}/v1/agents?limit=1000&order=asc",
        token=token,
    )
    return _agents_from_response(response.body)


def goal_planner_note() -> str:
    return (
        f"{GOAL_PLANNER_MARKER}\n"
        "- Run goal-planning as an interview: ask one concise question at a time, "
        "then synthesize a complete draft.\n"
        "- Use AskUserQuestion for structured choices and required clarification "
        "when the harness exposes it; otherwise ask directly in chat.\n"
        "- Scopes are organization, department, and individual employee agents. "
        "Do not use workflow agents as employee targets.\n"
        "- Use bytedesk_jira and bytedesk_confluence to search/read context before "
        "creating or recommending tracked work. Reference Google Workspace only "
        "when an available source is provided by the session.\n"
        "- The final draft must include title, priority, readiness_kind, "
        "dependencies, desired outcome, acceptance criteria, assumptions, and "
        "source references.\n"
        "- Do not call goal_create until the user explicitly approves the final "
        "draft."
    )


def merge_goal_planner_note(prompt: str, note: str) -> tuple[str, bool]:
    if GOAL_PLANNER_MARKER not in prompt:
        return f"{prompt.rstrip()}\n\n{note}\n", True
    before, marker, _after = prompt.partition(GOAL_PLANNER_MARKER)
    merged = f"{before.rstrip()}\n\n{marker}{note.removeprefix(GOAL_PLANNER_MARKER)}\n"
    return merged, merged != prompt


def _builtin_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
        return entry["name"]
    return None


def ensure_goal_planner_config(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
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
    if params.get("goalPlanner") is not True:
        params["goalPlanner"] = True
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
    for builtin in GOAL_PLANNER_BUILTINS:
        if builtin not in builtin_names:
            next_builtins.append(builtin)
            changed = True
    if tools.get("builtins") != next_builtins:
        tools["builtins"] = next_builtins
        updated["tools"] = tools
        changed = True

    note = goal_planner_note()
    prompt = updated.get("prompt")
    if isinstance(prompt, str):
        next_prompt, prompt_changed = merge_goal_planner_note(prompt, note)
        if prompt_changed:
            updated["prompt"] = next_prompt
            changed = True
    elif prompt is None:
        updated["prompt"] = f"{note}\n"
        changed = True
    else:
        raise ValueError("config.prompt must be a string when present")

    return updated, changed


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
        help="Only update this agent id; repeatable. Defaults to goal-planner/chief-of-staff.",
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
    updated_count = 0
    unchanged_count = 0
    skipped_count = 0
    skipped_workflows: list[str] = []

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
        if _is_workflow(agent) and agent_id not in explicit_ids:
            skipped_count += 1
            skipped_workflows.append(agent_id)
            continue

        image, etag = get_agent_image(args.server, token, agent_id)
        config = image.get("config")
        if not isinstance(config, dict):
            raise ApiError(f"{agent_id}: image config was not an object")

        next_config, changed = ensure_goal_planner_config(config)
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
                "skipped_workflows": skipped_workflows,
                "missing": missing,
                "dry_run": args.dry_run,
            },
            sort_keys=True,
        )
    )
    return 1 if explicit_ids and missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
