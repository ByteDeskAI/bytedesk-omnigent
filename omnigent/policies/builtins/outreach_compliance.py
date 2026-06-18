"""Built-in outreach compliance gate (BDP-2278 F3, ADR-0142).

The CAN-SPAM / GDPR floor for agent-driven outreach: a tool call matching an
outreach pattern (an email / SMS send) must carry an unsubscribe / opt-out
mechanism, and its recipient must not be on the do-not-contact suppression list
(``omnigent/compliance.py``). A missing unsubscribe is an unlawful send → DENY
(it cannot be approved away); a suppressed recipient → DENY. The unsubscribe
check is stateless (arg inspection); suppression enforcement is wired via an
injectable ``is_suppressed`` checker so the policy stays unit-provable. Mirrors
``forever_gate`` / ``spawn_governor``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# Argument keys that satisfy the CAN-SPAM unsubscribe requirement.
_UNSUB_KEYS = ("unsubscribe", "unsubscribe_url", "opt_out", "list_unsubscribe")


def outreach_compliance(
    patterns: list[str],
    *,
    recipient_arg: str = "to",
    channel: str = "email",
    require_unsubscribe: bool = True,
    is_suppressed: Callable[..., bool] | None = None,
) -> PolicyCallable:
    """Factory: DENY a matched outreach call that is unlawful or suppressed.

    :param patterns: Regex patterns (``re.search`` against the tool name) of
        outreach tools (e.g. ``["email\\.send", "sms\\.send"]``).
    :param recipient_arg: Argument key carrying the recipient address.
    :param channel: Suppression channel to check the recipient against.
    :param require_unsubscribe: DENY when no unsubscribe/opt-out arg is present.
    :param is_suppressed: Optional ``(channel=, address=) -> bool`` checker (wired
        in-process to the suppression store); when omitted only the unsubscribe
        check runs.
    :returns: A policy callable enforcing the outreach compliance floor.
    """
    compiled = [re.compile(p) for p in patterns]

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") != "tool_call":
            return _ALLOW
        data = event.get("data") or {}
        name = data.get("name", "")
        if not any(pat.search(name) for pat in compiled):
            return _ALLOW
        args = data.get("arguments") or {}

        if require_unsubscribe and not any(args.get(k) for k in _UNSUB_KEYS):
            return {
                "result": "DENY",
                "reason": (
                    f"outreach '{name}' has no unsubscribe/opt-out — CAN-SPAM "
                    "requires one in every commercial message (ADR-0142)"
                ),
            }

        if is_suppressed is not None:
            recipient = args.get(recipient_arg)
            if recipient and is_suppressed(channel=channel, address=str(recipient)):
                return {
                    "result": "DENY",
                    "reason": (
                        f"recipient '{recipient}' is on the do-not-contact list — "
                        "must not be contacted (opt-out / GDPR erasure, ADR-0142)"
                    ),
                }

        return _ALLOW

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.outreach_compliance.outreach_compliance",
        "kind": "factory",
        "name": "Outreach Compliance Gate",
        "description": "Denies a matched outreach tool call with no unsubscribe/opt-out "
        "(CAN-SPAM) and (when the suppression checker is wired) a do-not-contact "
        "recipient (GDPR/opt-out), ADR-0142.",
        "params_schema": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regex patterns of outreach tool names",
                },
                "recipient_arg": {
                    "type": "string",
                    "description": "Argument key carrying the recipient address",
                    "default": "to",
                },
                "channel": {
                    "type": "string",
                    "description": "Suppression channel to check the recipient against",
                    "default": "email",
                },
                "require_unsubscribe": {
                    "type": "boolean",
                    "description": "DENY when no unsubscribe/opt-out arg is present",
                    "default": True,
                },
            },
            "required": ["patterns"],
        },
    },
]
