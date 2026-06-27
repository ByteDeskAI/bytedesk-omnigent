"""Goal-engine boot-state observability (BDP-2599, Wave 6).

``goal_engine_boot_summary`` is a pure, testable dict of the engine's operating
state at loop startup — autonomy posture, coordination backplane mode, registered
providers, arming-enabled, tick interval. The loop logs it once so an operator can
see at a glance whether the org is armed + multi-replica-safe + which providers
are wired. ``armed`` is the Wave-5 gate readback: full_auto posture AND the
``BYTEDESK_GOALS_ARMING_ENABLED`` flag both hold.
"""
from __future__ import annotations

from typing import Any

_BACKPLANE_NAMES = {"NatsBackplane": "nats", "InProcessBackplane": "inprocess"}


def goal_engine_boot_summary(
    *,
    config: Any,
    backplane: Any | None,
    provider_registry: Any,
    arming_enabled: bool,
    interval_seconds: int,
) -> dict[str, Any]:
    """One-shot readback of engine operating state (pure → unit-provable)."""
    posture = getattr(config, "autonomy_posture", "gated")
    # No backplane started → in-process (single-replica) default.
    backplane_mode = (
        _BACKPLANE_NAMES.get(type(backplane).__name__, type(backplane).__name__)
        if backplane is not None
        else "inprocess"
    )
    return {
        "autonomy_posture": posture,
        "armed": posture == "full_auto" and arming_enabled,
        "arming_enabled": arming_enabled,
        "backplane": backplane_mode,
        "providers": [p.name for p in provider_registry.providers()],
        "tick_interval_seconds": interval_seconds,
    }


__all__ = ["goal_engine_boot_summary"]
