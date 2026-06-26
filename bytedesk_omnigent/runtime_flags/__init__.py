"""Runtime feature flags for ByteDesk Omnigent.

The public contribution point is :class:`BytedeskRuntimeFlagsExtension`, which is
registered through the Omnigent extension-author SDK. Keep new surfaces in this
package and expose them through ``omnigent.sdk`` decorators rather than editing
core route lists directly.
"""

from __future__ import annotations

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
]
