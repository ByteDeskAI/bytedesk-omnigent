#!/usr/bin/env python3
"""Persist Agentic Inbox MCP access into ByteDesk persona agent images.

This script intentionally updates agents through Omnigent's
``/v1/agents/{agent_id}/image`` API instead of editing seed YAML files
directly. That API rebuilds and stores the packaged agent bundle, bumps
the agent version, warm-swaps the cache, and marks the agent image as
``sot_tier=migrated`` so startup seeding will not overwrite it.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL = "http://omnigent.bytedesk.localhost"
DEFAULT_EMAIL_DOMAIN = "agents.dev.bytedesk.ai"
DEFAULT_MCP_URL = "${AGENTIC_INBOX_MCP_URL}"
TOOL_NAME = "agentic-inbox"
EMAIL_NOTE_MARKER = "EMAIL ACCOUNT (agentic-inbox)"
ACCESS_CLIENT_ID_REF = "${AGENTIC_INBOX_CF_ACCESS_CLIENT_ID}"
ACCESS_CLIENT_SECRET_REF = "${AGENTIC_INBOX_CF_ACCESS_CLIENT_SECRET}"
ALLOWED_TOOLS = [
    "list_emails",
    "get_email",
    "get_thread",
    "search_emails",
    "draft_reply",
    "create_draft",
    "update_draft",
    "send_reply",
    "send_email",
    "delete_email",
    "mark_email_read",
    "move_email",
]


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: Any


class ApiError(RuntimeError):
    """Raised when an Omnigent API request fails."""


def _json_request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any | None = None,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        **(headers or {}),
    }
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=payload,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            decoded = json.loads(raw.decode("utf-8")) if raw else None
            return HttpResponse(resp.status, dict(resp.headers), decoded)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"{method} {url} failed with HTTP {exc.code}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"{method} {url} failed: {exc.reason}") from exc


def login(base_url: str, username: str, password: str) -> str:
    response = _json_request(
        "POST",
        f"{base_url.rstrip('/')}/auth/login",
        body={"username": username, "password": password},
    )
    token = response.body.get("token") if isinstance(response.body, dict) else None
    if not isinstance(token, str) or not token:
        raise ApiError("auth/login response did not include a bearer token")
    return token


def display_name_to_email(display_name: str, domain: str) -> str:
    words = re.findall(r"[a-z0-9]+", display_name.lower())
    if len(words) < 2:
        raise ValueError(
            f"display name must contain at least first and last names: {display_name!r}"
        )
    return f"{words[0]}.{words[-1]}@{domain}"


def _is_workflow(agent: dict[str, Any]) -> bool:
    if agent.get("workflow") is True:
        return True
    params = agent.get("params")
    if isinstance(params, dict) and params.get("workflow") is True:
        return True
    return False


def persona_agents(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        agents = payload.get("agents") or payload.get("items") or payload.get("data") or []
    else:
        agents = payload
    if not isinstance(agents, list):
        raise ValueError("agents response did not contain a list")
    return [
        agent
        for agent in agents
        if isinstance(agent, dict)
        and isinstance(agent.get("display_name"), str)
        and agent["display_name"].strip()
        and not _is_workflow(agent)
    ]


def email_note(email: str) -> str:
    return (
        f"{EMAIL_NOTE_MARKER}\n"
        f"- Your personal email address is {email}.\n"
        f"- Use the {TOOL_NAME} MCP server for email operations.\n"
        f"- For every {TOOL_NAME} tool call, set mailboxId to {email}.\n"
        "- Do not read from, search, draft from, or send from another mailbox "
        "unless Ryan explicitly asks."
    )


def merge_email_note(prompt: str, note: str) -> tuple[str, bool]:
    if EMAIL_NOTE_MARKER not in prompt:
        return f"{prompt.rstrip()}\n\n{note}\n", True
    before, marker, _after = prompt.partition(EMAIL_NOTE_MARKER)
    merged = f"{before.rstrip()}\n\n{marker}{note.removeprefix(EMAIL_NOTE_MARKER)}\n"
    return merged, merged != prompt


def ensure_agentic_inbox_config(
    config: dict[str, Any],
    *,
    display_name: str,
    email: str,
    mcp_url: str,
) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(config)
    changed = False

    params = updated.get("params")
    if not isinstance(params, dict):
        params = {}
    desired_params = {
        "email": email,
        "mailboxId": email,
    }
    for key, value in desired_params.items():
        if params.get(key) != value:
            params[key] = value
            changed = True
    if updated.get("params") != params:
        updated["params"] = params
        changed = True

    tools = updated.get("tools")
    if tools is None:
        tools = {}
    if not isinstance(tools, dict):
        raise ValueError(f"{display_name}: config.tools must be a mapping when present")
    desired_tool = {
        "type": "mcp",
        "url": mcp_url,
        "headers": {
            "CF-Access-Client-Id": ACCESS_CLIENT_ID_REF,
            "CF-Access-Client-Secret": ACCESS_CLIENT_SECRET_REF,
        },
        "tool_allowlist": ALLOWED_TOOLS,
    }
    if tools.get(TOOL_NAME) != desired_tool:
        tools[TOOL_NAME] = desired_tool
        updated["tools"] = tools
        changed = True

    note = email_note(email)
    prompt = updated.get("prompt")
    if isinstance(prompt, str):
        next_prompt, prompt_changed = merge_email_note(prompt, note)
        if prompt_changed:
            updated["prompt"] = next_prompt
            changed = True
    elif prompt is None:
        updated["prompt"] = f"{note}\n"
        changed = True
    else:
        raise ValueError(f"{display_name}: config.prompt must be a string when present")

    return updated, changed


def list_agents(base_url: str, token: str) -> list[dict[str, Any]]:
    response = _json_request(
        "GET",
        f"{base_url.rstrip('/')}/v1/agents?limit=1000&order=asc",
        token=token,
    )
    return persona_agents(response.body)


def get_agent_image(base_url: str, token: str, agent_id: str) -> tuple[dict[str, Any], str | None]:
    response = _json_request(
        "GET",
        f"{base_url.rstrip('/')}/v1/agents/{agent_id}/image",
        token=token,
    )
    if not isinstance(response.body, dict):
        raise ApiError(f"image response for {agent_id} was not an object")
    return response.body, response.headers.get("ETag")


def put_agent_image(
    base_url: str,
    token: str,
    agent_id: str,
    config: dict[str, Any],
    etag: str | None,
) -> None:
    headers = {"If-Match": etag} if etag else {}
    _json_request(
        "PUT",
        f"{base_url.rstrip('/')}/v1/agents/{agent_id}/image",
        token=token,
        body={"config": config},
        headers=headers,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.getenv("OMNIGENT_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username", default=os.getenv("OMNIGENT_USERNAME"))
    parser.add_argument("--password", default=os.getenv("OMNIGENT_PASSWORD"))
    parser.add_argument("--token", default=os.getenv("OMNIGENT_BEARER_TOKEN"))
    parser.add_argument(
        "--email-domain",
        default=os.getenv("AGENTIC_INBOX_EMAIL_DOMAIN", DEFAULT_EMAIL_DOMAIN),
    )
    parser.add_argument("--mcp-url", default=os.getenv("AGENTIC_INBOX_MCP_URL", DEFAULT_MCP_URL))
    parser.add_argument(
        "--agent-id",
        action="append",
        default=[],
        help="Only update this agent id; repeatable",
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

    wanted = set(args.agent_id)
    updated_count = 0
    unchanged_count = 0
    skipped_count = 0

    for agent in list_agents(args.server, token):
        agent_id = agent.get("id")
        display_name = agent.get("display_name")
        if not isinstance(agent_id, str) or not isinstance(display_name, str):
            skipped_count += 1
            continue
        if wanted and agent_id not in wanted:
            skipped_count += 1
            continue

        email = display_name_to_email(display_name, args.email_domain)
        image, etag = get_agent_image(args.server, token, agent_id)
        config = image.get("config")
        if not isinstance(config, dict):
            raise ApiError(f"{agent_id}: image config was not an object")

        next_config, changed = ensure_agentic_inbox_config(
            config,
            display_name=display_name,
            email=email,
            mcp_url=args.mcp_url,
        )
        if not changed:
            unchanged_count += 1
            print(f"unchanged {agent_id} {display_name} {email}")
            continue
        if args.dry_run:
            updated_count += 1
            print(f"would-update {agent_id} {display_name} {email}")
            continue
        put_agent_image(args.server, token, agent_id, next_config, etag)
        updated_count += 1
        print(f"updated {agent_id} {display_name} {email}")

    print(
        json.dumps(
            {
                "updated": updated_count,
                "unchanged": unchanged_count,
                "skipped": skipped_count,
                "dry_run": args.dry_run,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
