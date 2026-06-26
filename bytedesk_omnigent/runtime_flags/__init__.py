"""Runtime feature flags for ByteDesk Omnigent.

The public contribution point is :class:`BytedeskRuntimeFlagsExtension`, which is
registered through the Omnigent extension-author SDK. Keep new surfaces in this
package and expose them through ``omnigent.sdk`` decorators rather than editing
core route lists directly.
"""

from __future__ import annotations

from .defaults import (
    NATS_UPGRADE_FLAG_DEFINITIONS,
    NATS_UPGRADE_FLAG_KEYS,
    RUNTIME_MESSAGE_BUS_MODE,
    RUNTIME_PRESENCE_STORE,
    RUNTIME_REALTIME_PUBLISHER,
    RUNTIME_SESSION_EVENTS_MODE,
    RUNTIME_SESSION_INITIATOR,
    build_runtime_flag_context,
    seed_runtime_flag_defaults,
)
from .extension import BytedeskRuntimeFlagsExtension
from .models import (
    EvaluationContext,
    EvaluationResult,
    FlagDefinition,
    FlagDescriptor,
    FlagRule,
    FlagVariation,
    PercentageRollout,
    RolloutBucket,
)
from .store import InMemoryRuntimeFlagStore, NatsRuntimeFlagStore, RuntimeFlagStore

__all__ = [
    "NATS_UPGRADE_FLAG_DEFINITIONS",
    "NATS_UPGRADE_FLAG_KEYS",
    "RUNTIME_MESSAGE_BUS_MODE",
    "RUNTIME_PRESENCE_STORE",
    "RUNTIME_REALTIME_PUBLISHER",
    "RUNTIME_SESSION_EVENTS_MODE",
    "RUNTIME_SESSION_INITIATOR",
    "BytedeskRuntimeFlagsExtension",
    "EvaluationContext",
    "EvaluationResult",
    "FlagDefinition",
    "FlagDescriptor",
    "FlagRule",
    "FlagVariation",
    "InMemoryRuntimeFlagStore",
    "NatsRuntimeFlagStore",
    "PercentageRollout",
    "RolloutBucket",
    "RuntimeFlagStore",
    "build_runtime_flag_context",
    "seed_runtime_flag_defaults",
]
