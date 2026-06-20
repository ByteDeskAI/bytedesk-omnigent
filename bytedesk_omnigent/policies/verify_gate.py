"""Verify-as-gate policy (BDP-2276 E3, ADR-0142).

Enforces that a Confirm/Destructive action proves its own outcome instead of
merely *claiming* success. A gated tool (a deploy, a delete, an external send)
must self-report machine verification in its result — the convention is a
truthy ``verified`` field. A gated tool that returns *success* WITHOUT that
proof is a ``claimed_unverified`` outcome: the policy DENIES the result so the
harness surfaces it back to the agent (which must re-run with a real
verification step), rather than letting an unproven destructive claim stand.

Fires on the ``tool_result`` phase. The :class:`~omnigent.policies.schema.PolicyEvent`
``type`` Literal is closed — there is no ``verify`` event type, so the gate
hooks the existing post-action phase. Non-gated tools, explicit error results,
and already-verified results all pass through (abstain).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bytedesk_omnigent.policies import PolicyRegistryRaw
from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_log = logging.getLogger(__name__)

_DEFAULT_VERIFIED_FIELD = "verified"


def _result_payload(data: Any) -> dict[str, Any] | None:
    """Return the tool result as a dict, or ``None`` when it is not object-shaped.

    The ``tool_result`` event carries ``data = {"result": <tool-output>}``. Our
    builtin tools return a JSON string, so the result is either an already-parsed
    dict or a JSON-object string. Anything else (a bare string, a list, a number)
    is *not* a verifiable outcome object.

    :param data: The event ``data`` payload.
    :returns: The parsed result dict, or ``None``.
    """
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def verify_as_gate(
    *,
    gated_tools: list[str],
    verified_field: str = _DEFAULT_VERIFIED_FIELD,
) -> PolicyCallable:
    """Factory: deny a gated tool's success result unless it is machine-verified.

    Fires on ``tool_result`` events. When the result is for one of the
    *gated_tools* (the Confirm/Destructive set), the result must carry a truthy
    *verified_field* to prove the action's effect was machine-checked. A gated
    tool that returns a non-error result without that proof is denied with a
    ``claimed_unverified`` reason.

    Fail-closed: a gated tool whose result is not a verifiable object is also
    denied — a destructive action that reports nothing checkable cannot be
    trusted to have done what it claimed.

    :param gated_tools: Tool names that perform a confirm/destructive action and
        must prove their outcome, e.g.
        ``["bytedesk_release_trigger", "google_workspace_drive_delete"]``.
        Required — the operator lists which tools to gate.
    :param verified_field: The result field that, when truthy, marks the outcome
        as machine-verified. Defaults to ``"verified"``.
    :returns: An async policy callable that denies unverified gated outcomes.
    """
    gated = frozenset(gated_tools)

    async def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """Deny a gated tool's unverified success outcome.

        :param event: Policy event dict.
        :returns: DENY when a gated tool's outcome is not machine-verified;
            ``None`` (abstain) for non-gated tools, error results, and verified
            results.
        """
        if event.get("type") != "tool_result":
            return None

        tool = event.get("target")
        if tool not in gated:
            return None

        payload = _result_payload(event.get("data"))
        if payload is None:
            _log.info(
                "verify_as_gate: gated tool %s returned a non-verifiable result — "
                "denying as claimed_unverified",
                tool,
            )
            return {
                "result": "DENY",
                "reason": (
                    f"'{tool}' is a gated destructive/confirm action but its result "
                    f"is not a verifiable object — the outcome is claimed_unverified. "
                    f"Re-run so the action reports a checkable '{verified_field}' result."
                ),
            }

        # An explicit error result is not a success claim — let the harness
        # surface the failure to the agent normally.
        if payload.get("error"):
            return None

        if payload.get(verified_field) is True:
            return None

        _log.info(
            "verify_as_gate: gated tool %s reported success without '%s' — "
            "denying as claimed_unverified",
            tool,
            verified_field,
        )
        return {
            "result": "DENY",
            "reason": (
                f"'{tool}' reported success without machine verification "
                f"('{verified_field}' is not true) — the outcome is claimed_unverified. "
                f"Run the matching verify step so the result can stand."
            ),
        }

    return evaluate  # type: ignore[return-value]


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[PolicyRegistryRaw] = [
    {
        "handler": "bytedesk_omnigent.policies.verify_gate.verify_as_gate",
        "kind": "factory",
        "name": "Verify Destructive Outcomes (verify-as-gate)",
        "description": (
            "Requires Confirm/Destructive tools to prove their outcome. On a "
            "gated tool's result, the action must self-report a truthy 'verified' "
            "field; a success claim without it is denied as 'claimed_unverified' "
            "so unproven destructive actions never stand."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "gated_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tool names that perform a confirm/destructive action and "
                        "must prove their outcome, e.g. ['bytedesk_release_trigger']."
                    ),
                },
                "verified_field": {
                    "type": "string",
                    "description": (
                        "Result field that marks the outcome machine-verified "
                        "when truthy. Defaults to 'verified'."
                    ),
                },
            },
            "required": ["gated_tools"],
        },
    },
]
