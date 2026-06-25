#!/usr/bin/env python3
"""Persist GitHub MCP access into selected ByteDesk engineering agent images."""

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
    get_agent_image,
    list_agents,
    login,
    put_agent_image,
)

DEFAULT_BASE_URL = "http://omnigent.bytedesk.localhost"
DEFAULT_MCP_URL = "${GITHUB_MCP_URL}"
TOOL_NAME = "github"
GITHUB_NOTE_MARKER = "GITHUB ACCESS (github MCP)"
ENGINEERING_AGENT_TARGETS = {
    "platform-architect": "Elias Mercer",
    "backend-development-lead": "Priya Nair",
    "web-development-lead": "Nolan Price",
    "bytedesk-platform-developer": "Sam Whitaker",
    "quality-and-release-lead": "Elena Torres",
}
ENGINEERING_AGENT_IDS = set(ENGINEERING_AGENT_TARGETS)
ENGINEERING_AGENT_DISPLAY_NAMES = {
    display_name: agent_id for agent_id, display_name in ENGINEERING_AGENT_TARGETS.items()
}
ALLOWED_TOOLS = [
    "get_file_contents",
    "create_or_update_file",
    "list_branches",
    "create_branch",
    "create_pull_request",
    "get_pull_request",
    "list_pull_requests",
    "merge_pull_request",
    "create_issue",
    "list_issues",
    "add_issue_comment",
    "search_code",
    "search_repositories",
    "list_commits",
    "get_commit",
]


def github_note() -> str:
    return (
        f"{GITHUB_NOTE_MARKER}\n"
        "- Use the github MCP server for ByteDesk GitHub repository work.\n"
        "- Pass repo as owner/name, for example ByteDeskAI/bytedesk-platform.\n"
        "- Prefer creating feature branches and pull requests over direct writes to "
        "protected branches.\n"
        "- Treat merge_pull_request as a finalization tool: use it only when the task "
        "explicitly asks for merge/land and branch protection is satisfied."
    )


def merge_github_note(prompt: str, note: str) -> tuple[str, bool]:
    if GITHUB_NOTE_MARKER not in prompt:
        return f"{prompt.rstrip()}\n\n{note}\n", True
    before, marker, _after = prompt.partition(GITHUB_NOTE_MARKER)
    merged = f"{before.rstrip()}\n\n{marker}{note.removeprefix(GITHUB_NOTE_MARKER)}\n"
    return merged, merged != prompt


def ensure_github_mcp_config(
    config: dict[str, Any], *, mcp_url: str
) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(config)
    changed = False

    tools = updated.get("tools")
    if tools is None:
        tools = {}
    if not isinstance(tools, dict):
        raise ValueError("config.tools must be a mapping when present")
    desired_tool = {
        "type": "mcp",
        "url": mcp_url,
        "tool_allowlist": ALLOWED_TOOLS,
    }
    if tools.get(TOOL_NAME) != desired_tool:
        tools[TOOL_NAME] = desired_tool
        updated["tools"] = tools
        changed = True

    note = github_note()
    prompt = updated.get("prompt")
    if isinstance(prompt, str):
        next_prompt, prompt_changed = merge_github_note(prompt, note)
        if prompt_changed:
            updated["prompt"] = next_prompt
            changed = True
    elif prompt is None:
        updated["prompt"] = f"{note}\n"
        changed = True
    else:
        raise ValueError("config.prompt must be a string when present")

    return updated, changed


def engineering_target_id(agent_id: str, display_name: str | None) -> str | None:
    if agent_id in ENGINEERING_AGENT_IDS:
        return agent_id
    if display_name:
        return ENGINEERING_AGENT_DISPLAY_NAMES.get(display_name)
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.getenv("OMNIGENT_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username", default=os.getenv("OMNIGENT_USERNAME"))
    parser.add_argument("--password", default=os.getenv("OMNIGENT_PASSWORD"))
    parser.add_argument("--token", default=os.getenv("OMNIGENT_BEARER_TOKEN"))
    parser.add_argument("--mcp-url", default=os.getenv("GITHUB_MCP_URL", DEFAULT_MCP_URL))
    parser.add_argument(
        "--agent-id",
        action="append",
        default=[],
        help="Only update this agent id; repeatable. Defaults to engineering personas.",
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

    explicit_ids = set(args.agent_id)
    wanted = explicit_ids or ENGINEERING_AGENT_IDS
    found: set[str] = set()
    updated_count = 0
    unchanged_count = 0
    skipped_count = 0

    for agent in list_agents(args.server, token):
        agent_id = agent.get("id")
        display_name = agent.get("display_name")
        if not isinstance(agent_id, str):
            skipped_count += 1
            continue
        if explicit_ids:
            if agent_id not in wanted:
                skipped_count += 1
                continue
            found.add(agent_id)
        else:
            target_id = engineering_target_id(agent_id, display_name)
            if target_id not in wanted:
                skipped_count += 1
                continue
            found.add(target_id)
        image, etag = get_agent_image(args.server, token, agent_id)
        config = image.get("config")
        if not isinstance(config, dict):
            raise ApiError(f"{agent_id}: image config was not an object")

        next_config, changed = ensure_github_mcp_config(config, mcp_url=args.mcp_url)
        if not changed:
            unchanged_count += 1
            print(f"unchanged {agent_id} {display_name or ''}".rstrip())
            continue
        if args.dry_run:
            updated_count += 1
            print(f"would-update {agent_id} {display_name or ''}".rstrip())
            continue
        put_agent_image(args.server, token, agent_id, next_config, etag)
        updated_count += 1
        print(f"updated {agent_id} {display_name or ''}".rstrip())

    missing = sorted(wanted - found)
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
