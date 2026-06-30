#!/usr/bin/env python3
"""Persist Codex image generation access into design and marketing agent images.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from apply_agentic_inbox import (  # noqa: E402
    ApiError,
    _json_request,
    get_agent_image,
    login,
)

DEFAULT_BASE_URL = "http://omnigent.bytedesk.localhost"
IMAGEGEN_MARKER = "CODEX IMAGE GENERATION"
IMAGEGEN_BUILTIN = "bytedesk_generate_image"
IMAGEGEN_SKILL = "imagegen"
IMAGEGEN_SKILL_PATH = f"skills/{IMAGEGEN_SKILL}/SKILL.md"
CODEX_HARNESS = "codex"
CODEX_MODEL = "gpt-5.5"
WEB_PERSONA_NAMES = {
    "web-design-director",
    "web-development-lead",
}


@dataclass(frozen=True)
class TargetSelection:
    selected: list[dict[str, Any]]
    skipped_workflows: list[str]
    skipped_non_personas: list[str]


def _agents_from_response(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        agents = payload.get("agents") or payload.get("items") or payload.get("data") or []
    else:
        agents = payload
    if not isinstance(agents, list):
        raise ValueError("agents response did not contain a list")
    return [agent for agent in agents if isinstance(agent, dict)]


def _agent_params(agent: dict[str, Any]) -> dict[str, Any]:
    params = agent.get("params")
    return params if isinstance(params, dict) else {}


def _field(agent: dict[str, Any], key: str) -> str:
    value = agent.get(key)
    if isinstance(value, str):
        return value
    params_value = _agent_params(agent).get(key)
    return params_value if isinstance(params_value, str) else ""


def _identifier(agent: dict[str, Any]) -> str:
    for key in ("id", "name", "display_name"):
        value = agent.get(key)
        if isinstance(value, str) and value:
            return value
    return "<unknown>"


def _normal(value: str) -> str:
    return value.lower().replace("_", "-").strip()


def _is_workflow(agent: dict[str, Any]) -> bool:
    if agent.get("workflow") is True:
        return True
    params = _agent_params(agent)
    if params.get("workflow") is True:
        return True
    return isinstance(params.get("orchestrator"), str)


def _is_persona(agent: dict[str, Any]) -> bool:
    display_name = agent.get("display_name")
    return isinstance(display_name, str) and bool(display_name.strip())


def _matches_default_target(agent: dict[str, Any]) -> bool:
    name = _normal(_field(agent, "name"))
    title = _normal(_field(agent, "title"))
    department = _normal(_field(agent, "department"))

    if department == "marketing":
        return True
    if name in WEB_PERSONA_NAMES:
        return True
    title_text = title.replace("/", " ")
    return (
        "web design" in title_text
        or "web development" in title_text
        or "frontend" in title_text
        or "front-end" in title_text
    )


def select_target_agents(
    agents: list[dict[str, Any]],
    *,
    explicit_ids: set[str] | None = None,
) -> TargetSelection:
    explicit_ids = explicit_ids or set()
    selected: list[dict[str, Any]] = []
    skipped_workflows: list[str] = []
    skipped_non_personas: list[str] = []

    for agent in agents:
        agent_id = agent.get("id")
        if not isinstance(agent_id, str):
            continue
        explicit = agent_id in explicit_ids
        if explicit_ids and not explicit:
            continue
        if not explicit and not _matches_default_target(agent):
            continue
        if _is_workflow(agent) and not explicit:
            skipped_workflows.append(agent_id)
            continue
        if not _is_persona(agent) and not explicit:
            skipped_non_personas.append(agent_id)
            continue
        selected.append(agent)

    return TargetSelection(
        selected=selected,
        skipped_workflows=skipped_workflows,
        skipped_non_personas=skipped_non_personas,
    )


def image_generation_note() -> str:
    return (
        f"{IMAGEGEN_MARKER}\n"
        f"- This saved agent image is configured to run on the {CODEX_HARNESS} "
        f"harness with {CODEX_MODEL} so Codex-native image generation is "
        "available when the host Codex OAuth login is valid.\n"
        "- When a user asks for a concrete visual asset, design reference, "
        "campaign graphic, illustration, product mockup, or web/marketing "
        "imagery, use the bundled imagegen skill.\n"
        "- In Codex-native sessions, the imagegen skill uses the host Codex "
        "OAuth login. Do not ask for, expose, or store provider secrets.\n"
        "- If the current harness does not expose Codex-native image generation, "
        "call bytedesk_generate_image with a specific prompt and descriptive "
        "filename. Include intended format, aspect ratio, brand context, and "
        "any text restrictions when they matter.\n"
        "- Treat the returned file_id as the durable image asset reference for "
        "chat display, follow-up edits, downloads, and workflow handoff."
    )


def imagegen_skill_md() -> str:
    return (
        """---
name: imagegen
description: Generate or edit raster imagery for web design, web development, and marketing assets.
---
# Imagegen

Use this skill when the user needs a concrete raster visual: website imagery,
design references, mockups, campaign graphics, social creative, illustrations,
thumbnails, or edited image assets.

Codex-native path: in Codex-hosted sessions, use the built-in Codex image
generation surface exposed by the host CODEX_HOME. That host login is
OAuth-backed, so never ask for provider API keys and never print secrets.

Omnigent fallback path: if the current harness exposes tools but not
Codex-native image generation, call `bytedesk_generate_image` with a precise
prompt and a descriptive filename. Include aspect ratio, style, brand context,
subject matter, required text, and text restrictions when they matter.

Treat generated `file_id` values as durable ByteDesk asset references for chat
display, follow-up edits, download links, and workflow handoff.
"""
    )


def merge_image_generation_note(prompt: str, note: str) -> tuple[str, bool]:
    if IMAGEGEN_MARKER not in prompt:
        return f"{prompt.rstrip()}\n\n{note}\n", True
    before, marker, _after = prompt.partition(IMAGEGEN_MARKER)
    merged = f"{before.rstrip()}\n\n{marker}{note.removeprefix(IMAGEGEN_MARKER)}\n"
    return merged, merged != prompt


def _builtin_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
        return entry["name"]
    return None


def _ensure_imagegen_skill_filter(config: dict[str, Any]) -> bool:
    skills = config.get("skills")
    if skills == "all":
        return False
    if skills is None or skills == "none":
        config["skills"] = [IMAGEGEN_SKILL]
        return True
    if isinstance(skills, list):
        if IMAGEGEN_SKILL in skills:
            return False
        skills.append(IMAGEGEN_SKILL)
        return True
    raise ValueError("config.skills must be 'all', 'none', or a list when present")


def _ensure_codex_harness(config: dict[str, Any]) -> bool:
    executor = config.get("executor")
    if executor is None:
        executor = {}
    if not isinstance(executor, dict):
        raise ValueError("config.executor must be a mapping when present")

    executor_config = executor.get("config")
    if executor_config is None:
        executor_config = {}
    if not isinstance(executor_config, dict):
        raise ValueError("config.executor.config must be a mapping when present")

    changed = False
    if executor.get("type") != "omnigent":
        executor["type"] = "omnigent"
        changed = True
    if executor.get("model") != CODEX_MODEL:
        executor["model"] = CODEX_MODEL
        changed = True
    if executor_config.get("harness") != CODEX_HARNESS:
        executor_config["harness"] = CODEX_HARNESS
        changed = True
    if executor.get("config") != executor_config:
        executor["config"] = executor_config
        changed = True
    if config.get("executor") != executor:
        config["executor"] = executor
        changed = True
    return changed


def ensure_image_generation_config(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(config)
    changed = False

    if _ensure_codex_harness(updated):
        changed = True

    if _ensure_imagegen_skill_filter(updated):
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
    if IMAGEGEN_BUILTIN not in builtin_names:
        next_builtins.append({"name": IMAGEGEN_BUILTIN})
        changed = True

    if tools.get("builtins") != next_builtins:
        tools["builtins"] = next_builtins
        updated["tools"] = tools
        changed = True

    note = image_generation_note()
    prompt = updated.get("prompt")
    if isinstance(prompt, str):
        next_prompt, prompt_changed = merge_image_generation_note(prompt, note)
        if prompt_changed:
            updated["prompt"] = next_prompt
            changed = True
    elif prompt is None:
        updated["prompt"] = f"{note}\n"
        changed = True
    else:
        raise ValueError("config.prompt must be a string when present")

    return updated, changed


def build_image_update_body(
    config: dict[str, Any],
    *,
    existing_skills: list[str],
) -> tuple[dict[str, Any], bool]:
    next_config, changed = ensure_image_generation_config(config)
    body: dict[str, Any] = {"config": next_config}
    if IMAGEGEN_SKILL not in existing_skills:
        body["files"] = {IMAGEGEN_SKILL_PATH: imagegen_skill_md()}
        changed = True
    return body, changed


def put_agent_image_update(
    base_url: str,
    token: str,
    agent_id: str,
    body: dict[str, Any],
    etag: str | None,
) -> None:
    headers = {"If-Match": etag} if etag else {}
    _json_request(
        "PUT",
        f"{base_url.rstrip('/')}/v1/agents/{agent_id}/image",
        token=token,
        body=body,
        headers=headers,
    )


def list_all_agents(base_url: str, token: str) -> list[dict[str, Any]]:
    response = _json_request(
        "GET",
        f"{base_url.rstrip('/')}/v1/agents?limit=1000&order=asc",
        token=token,
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
        help=(
            "Only update this agent id; repeatable. Defaults to Marketing "
            "personas plus web-design/web-development personas."
        ),
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
    selection = select_target_agents(agents, explicit_ids=explicit_ids)
    found = {agent["id"] for agent in selection.selected if isinstance(agent.get("id"), str)}
    missing = sorted(explicit_ids - found)

    updated_count = 0
    unchanged_count = 0

    for agent in selection.selected:
        agent_id = agent.get("id")
        display_name = agent.get("display_name") or agent.get("name") or ""
        if not isinstance(agent_id, str):
            continue

        image, etag = get_agent_image(args.server, token, agent_id)
        config = image.get("config")
        if not isinstance(config, dict):
            raise ApiError(f"{agent_id}: image config was not an object")

        skills = image.get("skills")
        existing_skills = skills if isinstance(skills, list) else []
        body, changed = build_image_update_body(config, existing_skills=existing_skills)
        if not changed:
            unchanged_count += 1
            print(f"unchanged {agent_id} {display_name}".rstrip())
            continue
        if args.dry_run:
            updated_count += 1
            print(f"would-update {agent_id} {display_name}".rstrip())
            continue
        put_agent_image_update(args.server, token, agent_id, body, etag)
        updated_count += 1
        print(f"updated {agent_id} {display_name}".rstrip())

    skipped_count = max(len(agents) - len(selection.selected), 0)
    print(
        json.dumps(
            {
                "updated": updated_count,
                "unchanged": unchanged_count,
                "skipped": skipped_count,
                "skipped_workflows": selection.skipped_workflows,
                "skipped_non_personas": selection.skipped_non_personas,
                "missing": missing,
                "selected": [
                    {
                        "id": agent.get("id"),
                        "name": agent.get("name"),
                        "display_name": agent.get("display_name"),
                    }
                    for agent in selection.selected
                ],
                "dry_run": args.dry_run,
            },
            sort_keys=True,
        )
    )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
