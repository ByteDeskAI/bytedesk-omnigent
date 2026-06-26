"""Runner self-heal + host-failover tuning knobs (BDP-2579, ADR config table).

Six env-overridable values with the ADR defaults. Read once per heal via
:func:`load_runner_heal_config` (cheap; env reads). Mirrors the existing
module-constant + env style in ``sessions.py`` — no ConfigDescriptor plumbing
for internal timing knobs the operator tunes by env/Helm.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerHealConfig:
    """Resolved heal/failover config (see the ADR Configuration table)."""

    reconnect_hold_timeout_s: float = 8.0
    relaunch_max_attempts: int = 3
    relaunch_attempt_timeout_s: float = 10.0
    failover_enabled: bool = True
    failover_max_hops: int = 2
    failover_host_cooldown_s: float = 60.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_runner_heal_config() -> RunnerHealConfig:
    """Resolve the heal/failover knobs from env, falling back to ADR defaults."""
    return RunnerHealConfig(
        reconnect_hold_timeout_s=_env_float(
            "OMNIGENT_RUNNER_RECONNECT_HOLD_TIMEOUT_S", 8.0
        ),
        relaunch_max_attempts=_env_int("OMNIGENT_HOST_RELAUNCH_MAX_ATTEMPTS", 3),
        relaunch_attempt_timeout_s=_env_float(
            "OMNIGENT_HOST_RELAUNCH_ATTEMPT_TIMEOUT_S", 10.0
        ),
        failover_enabled=_env_bool("OMNIGENT_FAILOVER_ENABLED", True),
        failover_max_hops=_env_int("OMNIGENT_FAILOVER_MAX_HOPS", 2),
        failover_host_cooldown_s=_env_float("OMNIGENT_FAILOVER_HOST_COOLDOWN_S", 60.0),
    )


if __name__ == "__main__":  # pragma: no cover — runnable self-check
    assert load_runner_heal_config().failover_enabled is True
    os.environ["OMNIGENT_FAILOVER_ENABLED"] = "false"
    os.environ["OMNIGENT_FAILOVER_MAX_HOPS"] = "5"
    cfg = load_runner_heal_config()
    assert cfg.failover_enabled is False, cfg
    assert cfg.failover_max_hops == 5, cfg
    assert cfg.relaunch_max_attempts == 3, cfg  # default preserved
    print("runner_heal_config self-check OK")
