"""Built-in dry-run preview gate (BDP-2277 F6, ADR-0142).

Preview-on-ASK: for tool-name patterns that mutate the world (a deploy, a billing
charge, a customer-data write), park the call for approval **with a concrete
preview of exactly what would run** — the tool name + its arguments — so the
human approves the *specific* planned effect, not a blind "allow this tool?".
Surfaces the side effect before it happens (the "dry-run preview" half of the
governance suite). Stateless + pattern-matched, mirroring ``forever_gate`` /
``spawn_governor``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}


def dry_run_preview(patterns: list[str], max_preview_chars: int = 800) -> PolicyCallable:
    """Factory: ASK with a rendered preview of the planned call for matched tools.

    :param patterns: Regex patterns (``re.search`` against the tool name) of
        mutating tools whose effect must be previewed before it runs.
    :param max_preview_chars: Truncate the rendered arguments past this many chars.
    :returns: A policy callable that ASKs — carrying the concrete preview in the
        reason — for a matching tool call.
    """
    compiled = [re.compile(p) for p in patterns]

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") != "tool_call":
            return _ALLOW
        data = event.get("data") or {}
        name = data.get("name", "")
        if not any(pat.search(name) for pat in compiled):
            return _ALLOW
        args = data.get("arguments")
        try:
            rendered = (
                json.dumps(args, indent=2, default=str, sort_keys=True)
                if args
                else "(no arguments)"
            )
        except (TypeError, ValueError):
            rendered = str(args)
        if len(rendered) > max_preview_chars:
            rendered = rendered[:max_preview_chars] + "… (truncated)"
        return {
            "result": "ASK",
            "reason": (
                f"Dry-run preview — '{name}' will execute with:\n{rendered}\n"
                "Approve to run it for real (ADR-0142)."
            ),
        }

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "bytedesk_omnigent.policies.dry_run.dry_run_preview",
        "kind": "factory",
        "name": "Dry-Run Preview Gate",
        "description": "ASKs with a concrete preview (tool name + arguments) of a "
        "matched mutating tool call before it runs, so the approver sees the exact "
        "planned effect (ADR-0142).",
        "params_schema": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regex patterns of mutating tool names to preview",
                },
                "max_preview_chars": {
                    "type": "integer",
                    "description": "Truncate rendered arguments past this length",
                    "default": 800,
                },
            },
            "required": ["patterns"],
        },
    },
]
