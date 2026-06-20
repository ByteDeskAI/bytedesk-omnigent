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

from bytedesk_omnigent.policies import PolicyRegistryRaw
from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# Argument keys that satisfy the CAN-SPAM unsubscribe requirement.
_UNSUB_KEYS = ("unsubscribe", "unsubscribe_url", "opt_out", "list_unsubscribe")


def outreach_compliance(
    patterns: list[str],
    *,
    recipient_arg: str = "to",
    cc_keys: tuple[str, ...] = ("cc", "bcc"),
    channel: str = "email",
    require_unsubscribe: bool = True,
    is_suppressed: Callable[..., bool] | None = None,
) -> PolicyCallable:
    """Factory: DENY a matched outreach call that is unlawful or suppressed.

    :param patterns: Regex patterns (``re.search`` against the tool name) of
        outreach tools (e.g. ``["email\\.send", "sms\\.send"]``).
    :param recipient_arg: Argument key carrying the primary recipient address(es).
    :param cc_keys: Additional recipient-bearing arg keys (cc/bcc) also checked.
    :param channel: Suppression channel to check the recipients against.
    :param require_unsubscribe: DENY when no unsubscribe/opt-out arg is present.
    :param is_suppressed: Optional ``(channel=, address=) -> bool`` checker. When
        omitted (the only path the JSON spec/``factory_params`` can take — a
        callable can't be serialized there) it self-resolves to the durable
        suppression store, so the do-not-contact gate is always live.
    :returns: A policy callable enforcing the outreach compliance floor.
    """
    compiled = [re.compile(p) for p in patterns]

    # Resolve the suppression checker. A missing checker previously made the whole
    # suppression block dead code on the production attach path (a suppressed /
    # GDPR-erased recipient was silently ALLOWed) — default it to the durable
    # store so suppression is actually enforced (BDP-2285).
    if is_suppressed is None:

        def _check_suppressed(*, channel: str, address: str) -> bool:
            from bytedesk_omnigent.compliance import get_suppression_store

            return get_suppression_store().is_suppressed(
                channel=channel, address=address
            )
    else:
        _check_suppressed = is_suppressed

    recipient_keys = (recipient_arg, *cc_keys)

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

        # Flatten every recipient-bearing arg (to/cc/bcc; lists, comma-joined
        # strings) into individual addresses and DENY if ANY is suppressed. Never
        # str()-coerce a collection into one opaque token (e.g. "['a@x.com']")
        # that can't match the normalized store — fail CLOSED (DENY) on any shape
        # we cannot parse into addresses (BDP-2285).
        addresses: list[str] = []
        for rkey in recipient_keys:
            val = args.get(rkey)
            if val is None:
                continue
            if isinstance(val, (list, tuple, set)):
                items: list = list(val)
            elif isinstance(val, str):
                items = [a for a in (p.strip() for p in val.split(",")) if a]
            else:
                return {
                    "result": "DENY",
                    "reason": (
                        f"outreach '{name}' recipient '{rkey}' has an unparseable "
                        "shape — failing closed (ADR-0142)"
                    ),
                }
            for item in items:
                if not isinstance(item, str):
                    return {
                        "result": "DENY",
                        "reason": (
                            f"outreach '{name}' recipient '{rkey}' contains a "
                            "non-string address — failing closed (ADR-0142)"
                        ),
                    }
                addresses.append(item)

        for addr in addresses:
            if _check_suppressed(channel=channel, address=addr):
                return {
                    "result": "DENY",
                    "reason": (
                        f"recipient '{addr}' is on the do-not-contact list — must "
                        "not be contacted (opt-out / GDPR erasure, ADR-0142)"
                    ),
                }

        return _ALLOW

    return evaluate  # type: ignore[return-value]


POLICY_REGISTRY: list[PolicyRegistryRaw] = [
    {
        "handler": "bytedesk_omnigent.policies.outreach_compliance.outreach_compliance",
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
                    "description": "Argument key carrying the primary recipient address(es)",
                    "default": "to",
                },
                "cc_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional recipient-bearing arg keys (cc/bcc) also checked",
                    "default": ["cc", "bcc"],
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
