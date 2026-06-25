#!/usr/bin/env python3
"""Persist Jira and Confluence tool access into ByteDesk persona agent images.

This updates agents through Omnigent's ``/v1/agents/{agent_id}/image`` API,
not by editing seed YAML. The image API rebuilds and stores the packaged agent
bundle so startup seeding does not overwrite the change.
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
    get_agent_image,
    list_agents,
    login,
    put_agent_image,
)

DEFAULT_BASE_URL = "http://omnigent.bytedesk.localhost"
ATLASSIAN_NOTE_MARKER = "ATLASSIAN ACCESS (Jira / Confluence)"
ATLASSIAN_BUILTINS = ["bytedesk_jira", "bytedesk_confluence"]


def atlassian_note() -> str:
    return (
        f"{ATLASSIAN_NOTE_MARKER}\n"
        "- Use bytedesk_jira for Jira issue search, read, comment, transition, "
        "and create operations.\n"
        "- Use bytedesk_confluence for Confluence page search, read, create, "
        "update, and comment operations.\n"
        "- Treat Atlassian as the team system of record: search/read first, "
        "then update existing issues or pages before creating duplicates.\n"
        "- Keep Jira status honest. Do not transition work to Done without "
        "evidence that the work is actually complete."
    )


def merge_atlassian_note(prompt: str, note: str) -> tuple[str, bool]:
    if ATLASSIAN_NOTE_MARKER not in prompt:
        return f"{prompt.rstrip()}\n\n{note}\n", True
    before, marker, _after = prompt.partition(ATLASSIAN_NOTE_MARKER)
    merged = f"{before.rstrip()}\n\n{marker}{note.removeprefix(ATLASSIAN_NOTE_MARKER)}\n"
    return merged, merged != prompt


def _builtin_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
        return entry["name"]
    return None


def ensure_atlassian_tools_config(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(config)
    changed = False

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
    for builtin in ATLASSIAN_BUILTINS:
        if builtin not in builtin_names:
            next_builtins.append(builtin)
            changed = True

    if tools.get("builtins") != next_builtins:
        tools["builtins"] = next_builtins
        updated["tools"] = tools
        changed = True

    note = atlassian_note()
    prompt = updated.get("prompt")
    if isinstance(prompt, str):
        next_prompt, prompt_changed = merge_atlassian_note(prompt, note)
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
        help="Only update this agent id; repeatable. Defaults to all persona agents.",
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
        if explicit_ids and agent_id not in explicit_ids:
            skipped_count += 1
            continue
        found.add(agent_id)

        image, etag = get_agent_image(args.server, token, agent_id)
        config = image.get("config")
        if not isinstance(config, dict):
            raise ApiError(f"{agent_id}: image config was not an object")

        next_config, changed = ensure_atlassian_tools_config(config)
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

    missing = sorted(explicit_ids - found)
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
