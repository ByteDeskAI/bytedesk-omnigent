"""Feature Toggles for the inbound-event pipeline (ADR-0155, BDP-2560).

Gates the new pipeline + each Strangler cutover with the **Omnigent Runtime Feature
Flags** system (NATS-KV, request-time ``store.evaluate``, percentage rollout, live
``/v1/flags`` flips with no restart) — never ad-hoc env vars. All default **off**:
the pipeline ships dark and each cutover route flips on independently. The
per-source cutover flags chain on the master via ``prerequisites`` so they can't
fire before the core is enabled.
"""

from __future__ import annotations

from collections.abc import Iterable

from bytedesk_omnigent.runtime_flags.defaults import (
    build_runtime_flag_context,
    seed_runtime_flag_defaults,
)
from bytedesk_omnigent.runtime_flags.models import (
    FlagDefinition,
    FlagDescriptor,
    FlagVariation,
)
from bytedesk_omnigent.runtime_flags.store import (
    RuntimeFlagStore,
    runtime_flag_store_from_env,
)

INBOUND_PIPELINE_ENABLED = "inbound.pipeline.enabled"
INBOUND_FEED_ENABLED = "inbound.feed.enabled"
INBOUND_CUTOVER_GOAL_DELIVERY = "inbound.cutover.goal_delivery"
INBOUND_CUTOVER_SIGNAL_BUS = "inbound.cutover.signal_bus"
INBOUND_CUTOVER_AGENTIC_INBOX = "inbound.cutover.agentic_inbox"
INBOUND_CUTOVER_PROVIDER = "inbound.cutover.provider"

_OWNER = "inbound-pipeline"
_TAGS = ("inbound-pipeline", "adr-0155")


def _bool_flag(
    key: str,
    *,
    description: str,
    prerequisites: dict[str, bool] | None = None,
    safety_tier: int = 2,
) -> FlagDefinition:
    return FlagDefinition(
        descriptor=FlagDescriptor(
            key=key,
            value_type="boolean",
            owner=_OWNER,
            default_value=False,
            off_value=False,
            description=description,
            lifecycle="active",
            safety_tier=safety_tier,
            tags=_TAGS,
            json_schema={"type": "boolean"},
        ),
        enabled=True,
        variations=(FlagVariation("on", True), FlagVariation("off", False)),
        default_variation="off",
        prerequisites=dict(prerequisites or {}),
    )


INBOUND_FLAG_DEFINITIONS = (
    _bool_flag(
        INBOUND_PIPELINE_ENABLED,
        description="Master toggle: route bodies call the generic inbound pipeline vs legacy paths.",
    ),
    _bool_flag(
        INBOUND_FEED_ENABLED,
        description="Wire-Tap realtime emit + /v1/inbound/events SSE + feed surfaces.",
    ),
    _bool_flag(
        INBOUND_CUTOVER_GOAL_DELIVERY,
        description="Strangler: goal-delivery route runs through the pipeline (canary).",
        prerequisites={INBOUND_PIPELINE_ENABLED: True},
    ),
    _bool_flag(
        INBOUND_CUTOVER_SIGNAL_BUS,
        description="Strangler: ingress/signal-bus route runs through the pipeline.",
        prerequisites={INBOUND_PIPELINE_ENABLED: True},
    ),
    _bool_flag(
        INBOUND_CUTOVER_AGENTIC_INBOX,
        description="Strangler: agentic-inbox (LIVE email) route runs through the pipeline; ramp via percentage rollout.",
        safety_tier=3,
        prerequisites={INBOUND_PIPELINE_ENABLED: True},
    ),
    _bool_flag(
        INBOUND_CUTOVER_PROVIDER,
        description="Connected-app provider canonical ingress (POST /v1/inbound/events) → pipeline.",
        prerequisites={INBOUND_PIPELINE_ENABLED: True},
    ),
)


async def seed_inbound_flags(
    store: RuntimeFlagStore | None = None,
    definitions: Iterable[FlagDefinition] = INBOUND_FLAG_DEFINITIONS,
) -> None:
    """Seed the inbound flags once (won't overwrite live edits). For boot background."""
    await seed_runtime_flag_defaults(store or runtime_flag_store_from_env(), definitions)


async def evaluate_inbound_flag(
    key: str,
    *,
    tenant: str | None = None,
    session: str | None = None,
    source: str | None = None,
    store: RuntimeFlagStore | None = None,
) -> bool:
    """Evaluate an inbound boolean flag at request time. ``False`` if unset/unknown."""
    flag_store = store or runtime_flag_store_from_env()
    context = build_runtime_flag_context(
        tenant=tenant, session=session, extra={"source": source}
    )
    try:
        result = await flag_store.evaluate(key, context)
    except Exception:  # noqa: BLE001 - an unseeded/unreachable flag must fail closed (off)
        return False
    return bool(result.value)
