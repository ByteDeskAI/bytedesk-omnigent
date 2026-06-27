"""Per-tenant goal-engine configuration (BDP-2589, Phase 7).

The engine knobs (posture, approval, paper-trading, interval, caps, risk decays,
anomaly threshold) live on the **Omnigent Runtime Feature Flags** plane — the same
plane ``inbound/flags.py`` uses: NATS-KV, request-time ``store.evaluate``,
hot-reloadable with no restart, ships-dark with SAFE defaults, ``safety_tier`` per
key, and **per-tenant via a ``tenant`` rule**. Booleans, an enum (posture), and
numbers all fit the flag model (``value_type`` boolean/string/number), so one
plane carries the whole config rather than splitting across two control planes.

``load_goal_engine_config(tenant_id)`` evaluates every knob against a context
carrying ``tenant=tenant_id`` and returns a frozen :class:`GoalEngineConfig`. A
tenant with no rule resolves to the global default; an unseeded/unreachable flag
fails closed to the SAFE default (mirrors ``evaluate_inbound_flag``).

**full_auto is never the default.** ``goals.autonomy.posture`` defaults to
``gated``; arming full-auto is an explicit per-tenant flip (a tenant rule or a
global default-variation change), and the Phase-3 safety layer (paper-trading,
blast-radius, circuit breaker) stays in force in either posture.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

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

_logger = logging.getLogger(__name__)

# -- flag keys -------------------------------------------------------------
GOAL_AUTONOMY_POSTURE = "goals.autonomy.posture"
GOAL_APPROVAL_HIGH_RISK_REQUIRED = "goals.approval.high_risk_required"
GOAL_REQUIRE_APPROVAL_ALL = "goals.approval.require_all"
GOAL_PAPER_TRADING_DEFAULT = "goals.paper_trading.default"
GOAL_TICK_INTERVAL_SECONDS = "goals.tick.interval_seconds"
GOAL_BUDGET_DEFAULT_CAP_CENTS = "goals.budget.default_cap_cents"
GOAL_RISK_DECAY_LOW = "goals.roi.risk_decay.low"
GOAL_RISK_DECAY_MEDIUM = "goals.roi.risk_decay.medium"
GOAL_RISK_DECAY_HIGH = "goals.roi.risk_decay.high"
GOAL_CIRCUIT_ANOMALY_THRESHOLD_CENTS = "goals.circuit.anomaly_threshold_cents"
# Per-tenant goal-attribute schema (JSON), absent → free-form attributes (back-compat).
GOAL_ATTRIBUTE_SCHEMA = "goals.attributes.schema"

POSTURES = ("gated", "full_auto")

_OWNER = "goal-engine"
_TAGS = ("goal-engine", "bdp-2589")

# Safe defaults — every value is today's behaviour.
_DEFAULT_INTERVAL_SECONDS = 30
_DEFAULT_CAP_CENTS = 0
_DEFAULT_DECAY = {"low": 1.0, "medium": 0.7, "high": 0.4}


def _bool_flag(
    key: str, *, default: bool, description: str, safety_tier: int = 2
) -> FlagDefinition:
    return FlagDefinition(
        descriptor=FlagDescriptor(
            key=key,
            value_type="boolean",
            owner=_OWNER,
            default_value=default,
            off_value=default,
            description=description,
            safety_tier=safety_tier,
            tags=_TAGS,
            json_schema={"type": "boolean"},
        ),
        enabled=True,
        variations=(FlagVariation("on", True), FlagVariation("off", False)),
        default_variation="on" if default else "off",
    )


def _number_flag(key: str, *, default: float, description: str) -> FlagDefinition:
    return FlagDefinition(
        descriptor=FlagDescriptor(
            key=key,
            value_type="number",
            owner=_OWNER,
            default_value=default,
            off_value=default,
            description=description,
            tags=_TAGS,
            json_schema={"type": "number"},
        ),
        enabled=True,
        variations=(FlagVariation("default", default),),
        default_variation="default",
    )


GOAL_ENGINE_FLAG_DEFINITIONS = (
    FlagDefinition(
        descriptor=FlagDescriptor(
            key=GOAL_AUTONOMY_POSTURE,
            value_type="string",
            owner=_OWNER,
            default_value="gated",
            off_value="gated",
            description=(
                "Autonomy posture. 'gated' (default) keeps per-action governance; "
                "'full_auto' arms fund+spawn within budget without per-action approval "
                "(blast-radius/high-risk STILL gated). Arming is an explicit per-tenant flip."
            ),
            safety_tier=3,  # the arm switch — high blast radius.
            tags=_TAGS,
            json_schema={"type": "string", "enum": list(POSTURES)},
        ),
        enabled=True,
        variations=tuple(FlagVariation(p, p) for p in POSTURES),
        default_variation="gated",
    ),
    _bool_flag(
        GOAL_APPROVAL_HIGH_RISK_REQUIRED,
        default=True,
        description="High-risk goals route to approval instead of auto-spawn (blast-radius gate).",
        safety_tier=3,
    ),
    _bool_flag(
        GOAL_REQUIRE_APPROVAL_ALL,
        default=False,
        description="Gated posture: route EVERY funded goal to approval (not only high-risk).",
    ),
    _bool_flag(
        GOAL_PAPER_TRADING_DEFAULT,
        default=True,
        description="New goals default to paper-trading (simulate, book nothing real).",
    ),
    _number_flag(
        GOAL_TICK_INTERVAL_SECONDS,
        default=_DEFAULT_INTERVAL_SECONDS,
        description="Seconds between portfolio ticks.",
    ),
    _number_flag(
        GOAL_BUDGET_DEFAULT_CAP_CENTS,
        default=_DEFAULT_CAP_CENTS,
        description="Default per-scope budget cap (cents) for scopes with no budget; 0=uncapped.",
    ),
    _number_flag(
        GOAL_RISK_DECAY_LOW, default=_DEFAULT_DECAY["low"],
        description="ROI risk-decay multiplier for low-risk goals.",
    ),
    _number_flag(
        GOAL_RISK_DECAY_MEDIUM, default=_DEFAULT_DECAY["medium"],
        description="ROI risk-decay multiplier for medium-risk goals.",
    ),
    _number_flag(
        GOAL_RISK_DECAY_HIGH, default=_DEFAULT_DECAY["high"],
        description="ROI risk-decay multiplier for high-risk goals.",
    ),
    _number_flag(
        GOAL_CIRCUIT_ANOMALY_THRESHOLD_CENTS, default=0,
        description="Default circuit-breaker anomaly threshold (cents); 0 = no auto-trip default.",
    ),
    FlagDefinition(
        descriptor=FlagDescriptor(
            key=GOAL_ATTRIBUTE_SCHEMA,
            value_type="json",
            owner=_OWNER,
            default_value={},
            off_value={},
            description=(
                "Per-tenant goal-attribute schema "
                "({'properties': {name: {'type': ...}}, 'additionalProperties': bool}). "
                "Empty = no schema = free-form attributes (back-compat)."
            ),
            tags=_TAGS,
            json_schema={"type": "object"},
        ),
        enabled=True,
        variations=(FlagVariation("default", {}),),
        default_variation="default",
    ),
)


@dataclass(frozen=True)
class GoalEngineConfig:
    """Resolved per-tenant goal-engine knobs (all SAFE defaults)."""

    autonomy_posture: str = "gated"
    high_risk_required: bool = True
    require_approval_all: bool = False
    paper_trading_default: bool = True
    tick_interval_seconds: int = _DEFAULT_INTERVAL_SECONDS
    budget_default_cap_cents: int = _DEFAULT_CAP_CENTS
    risk_decay: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_DECAY))
    anomaly_threshold_cents: int | None = None
    attribute_schema: dict[str, Any] | None = None


async def seed_goal_engine_flags(
    store: RuntimeFlagStore | None = None,
    definitions: Iterable[FlagDefinition] = GOAL_ENGINE_FLAG_DEFINITIONS,
) -> None:
    """Seed the goal-engine flags once (won't overwrite live edits). For boot."""
    await seed_runtime_flag_defaults(store or runtime_flag_store_from_env(), definitions)


async def _value(store: RuntimeFlagStore, key: str, tenant: str | None, default: Any) -> Any:
    """Evaluate one flag for ``tenant``; fall back to ``default`` if unset/unreachable."""
    context = build_runtime_flag_context(tenant=tenant, extra={"tenant": tenant})
    try:
        result = await store.evaluate(key, context)
    except Exception:  # noqa: BLE001 - an unseeded/unreachable flag fails closed (safe default)
        return default
    return result.value


async def load_goal_engine_config(
    tenant_id: str | None,
    *,
    store: RuntimeFlagStore | None = None,
) -> GoalEngineConfig:
    """Resolve the per-tenant config; tenant-less / unset keys yield safe defaults."""
    flag_store = store or runtime_flag_store_from_env()

    async def g(key: str, default: Any) -> Any:
        return await _value(flag_store, key, tenant_id, default)

    posture = await g(GOAL_AUTONOMY_POSTURE, "gated")
    if posture not in POSTURES:
        posture = "gated"  # never silently arm on a bad value
    threshold = int(await g(GOAL_CIRCUIT_ANOMALY_THRESHOLD_CENTS, 0))
    schema = await g(GOAL_ATTRIBUTE_SCHEMA, {})
    return GoalEngineConfig(
        autonomy_posture=posture,
        high_risk_required=bool(await g(GOAL_APPROVAL_HIGH_RISK_REQUIRED, True)),
        require_approval_all=bool(await g(GOAL_REQUIRE_APPROVAL_ALL, False)),
        paper_trading_default=bool(await g(GOAL_PAPER_TRADING_DEFAULT, True)),
        tick_interval_seconds=int(await g(GOAL_TICK_INTERVAL_SECONDS, _DEFAULT_INTERVAL_SECONDS)),
        budget_default_cap_cents=int(await g(GOAL_BUDGET_DEFAULT_CAP_CENTS, _DEFAULT_CAP_CENTS)),
        risk_decay={
            "low": float(await g(GOAL_RISK_DECAY_LOW, _DEFAULT_DECAY["low"])),
            "medium": float(await g(GOAL_RISK_DECAY_MEDIUM, _DEFAULT_DECAY["medium"])),
            "high": float(await g(GOAL_RISK_DECAY_HIGH, _DEFAULT_DECAY["high"])),
        },
        anomaly_threshold_cents=threshold or None,
        attribute_schema=schema or None,
    )


# -- attribute schema validation -------------------------------------------
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "boolean": bool,
    "string": str,
    "number": (int, float),
    "integer": int,
    "object": dict,
    "array": list,
}


def validate_goal_attributes(
    attributes: dict[str, Any] | None, *, schema: dict[str, Any] | None
) -> None:
    """Validate ``attributes`` against a per-tenant ``schema`` (raise ``ValueError``).

    ``schema`` is a small JSON-schema subset: ``properties`` (name → ``{"type": ...}``)
    and ``additionalProperties`` (default True). ``schema is None`` → no validation
    (free-form, back-compat). Empty/no attributes always pass.
    """
    if not schema:
        return
    attributes = attributes or {}
    properties = schema.get("properties") or {}
    allow_extra = schema.get("additionalProperties", True)
    for name, value in attributes.items():
        spec = properties.get(name)
        if spec is None:
            if allow_extra:
                continue
            raise ValueError(f"unknown goal attribute {name!r} (not in tenant schema)")
        expected = spec.get("type")
        if expected is None:
            continue
        py_type = _JSON_TYPES.get(expected)
        if py_type is None:
            continue
        # bool is an int subclass — exclude it from numeric/integer checks.
        if expected in ("number", "integer") and isinstance(value, bool):
            raise ValueError(f"goal attribute {name!r} expected {expected}, got boolean")
        if not isinstance(value, py_type):
            raise ValueError(
                f"goal attribute {name!r} expected type {expected}, got {type(value).__name__}"
            )


__all__ = [
    "GOAL_APPROVAL_HIGH_RISK_REQUIRED",
    "GOAL_ATTRIBUTE_SCHEMA",
    "GOAL_AUTONOMY_POSTURE",
    "GOAL_BUDGET_DEFAULT_CAP_CENTS",
    "GOAL_CIRCUIT_ANOMALY_THRESHOLD_CENTS",
    "GOAL_ENGINE_FLAG_DEFINITIONS",
    "GOAL_PAPER_TRADING_DEFAULT",
    "GOAL_REQUIRE_APPROVAL_ALL",
    "GOAL_RISK_DECAY_HIGH",
    "GOAL_RISK_DECAY_LOW",
    "GOAL_RISK_DECAY_MEDIUM",
    "GOAL_TICK_INTERVAL_SECONDS",
    "POSTURES",
    "GoalEngineConfig",
    "load_goal_engine_config",
    "seed_goal_engine_flags",
    "validate_goal_attributes",
]
