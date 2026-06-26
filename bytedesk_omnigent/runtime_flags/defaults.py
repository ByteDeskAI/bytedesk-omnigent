"""Seeded runtime flags for the NATS upgrade path."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import EvaluationContext, FlagDefinition, FlagDescriptor, FlagVariation
from .store import FlagConflictError, FlagRevision, RuntimeFlagStore

RUNTIME_MESSAGE_BUS_MODE = "runtime.message_bus.mode"
RUNTIME_SESSION_EVENTS_MODE = "runtime.session_events.mode"
RUNTIME_PRESENCE_STORE = "runtime.presence.store"
RUNTIME_REALTIME_PUBLISHER = "runtime.realtime.publisher"
RUNTIME_SESSION_INITIATOR = "runtime.session_initiator"

NATS_UPGRADE_FLAG_KEYS = (
    RUNTIME_MESSAGE_BUS_MODE,
    RUNTIME_SESSION_EVENTS_MODE,
    RUNTIME_PRESENCE_STORE,
    RUNTIME_REALTIME_PUBLISHER,
    RUNTIME_SESSION_INITIATOR,
)

_OWNER = "runtime"
_TAGS = ("nats-upgrade", "runtime-control")


def _mode_flag(
    key: str,
    *,
    default: str,
    modes: tuple[str, ...],
    description: str,
    safety_tier: int = 2,
) -> FlagDefinition:
    return FlagDefinition(
        descriptor=FlagDescriptor(
            key=key,
            value_type="string",
            owner=_OWNER,
            default_value=default,
            off_value=default,
            description=description,
            lifecycle="active",
            safety_tier=safety_tier,
            tags=_TAGS,
            json_schema={"type": "string", "enum": list(modes)},
        ),
        enabled=True,
        variations=tuple(FlagVariation(mode, mode) for mode in modes),
        default_variation=default,
    )


NATS_UPGRADE_FLAG_DEFINITIONS = (
    _mode_flag(
        RUNTIME_MESSAGE_BUS_MODE,
        default="inprocess",
        modes=("inprocess", "nats"),
        description=(
            "Selects the internal async message bus implementation. NATS is the "
            "multi-replica target; inprocess is the safe local/test default."
        ),
    ),
    _mode_flag(
        RUNTIME_SESSION_EVENTS_MODE,
        default="local",
        modes=("local", "nats_core", "jetstream"),
        description=(
            "Selects the session-event transport. JetStream is the durable replay "
            "target for high-value session/task events."
        ),
    ),
    _mode_flag(
        RUNTIME_PRESENCE_STORE,
        default="local",
        modes=("local", "nats_kv"),
        description="Selects the presence and ephemeral ownership state store.",
    ),
    _mode_flag(
        RUNTIME_REALTIME_PUBLISHER,
        default="redis",
        modes=("redis", "dual", "nats"),
        description=(
            "Selects realtime fan-out publication. Dual mode supports parity "
            "measurement before cutting Redis paths over to NATS."
        ),
        safety_tier=3,
    ),
    _mode_flag(
        RUNTIME_SESSION_INITIATOR,
        default="http",
        modes=("http", "nats"),
        description=(
            "Selects how background schedulers initiate new sessions. HTTP keeps "
            "the current self-call path; NATS is the internal request/reply target."
        ),
        safety_tier=3,
    ),
)


def build_runtime_flag_context(
    *,
    environment: str | None = None,
    tenant: str | None = None,
    user: str | None = None,
    session: str | None = None,
    agent_id: str | None = None,
    runner_id: str | None = None,
    replica_id: str | None = None,
    target_key: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> EvaluationContext:
    """Build the stable evaluation context used by rollout decisions."""

    attrs: dict[str, Any] = {}
    for key, value in (
        ("environment", environment),
        ("tenant", tenant),
        ("user", user),
        ("session", session),
        ("agent_id", agent_id),
        ("runner_id", runner_id),
        ("replica_id", replica_id),
        ("key", target_key),
    ):
        if value is not None:
            attrs[key] = value
    if extra:
        attrs.update({str(key): value for key, value in extra.items() if value is not None})
    return EvaluationContext(attributes=attrs)


async def seed_runtime_flag_defaults(
    store: RuntimeFlagStore,
    definitions: Iterable[FlagDefinition] = NATS_UPGRADE_FLAG_DEFINITIONS,
) -> list[FlagRevision]:
    """Create missing runtime flag defaults without overwriting live edits."""

    created: list[FlagRevision] = []
    for definition in definitions:
        try:
            created.append(await store.upsert(definition, if_match=0))
        except FlagConflictError:
            continue
    return created
