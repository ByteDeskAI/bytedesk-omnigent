"""Safety-floor validators for the built-in policy factories (BDP-2411, ADR-0150).

Several safety policies could be silently weakened or disabled by a mis-set
factory param — a two-key gate that needs only one approver, a cost breaker with
no finite ceiling, an outreach gate with the legal unsubscribe requirement turned
off, a spawn governor with an effectively-infinite cap. These guards enforce the
non-negotiable floor at **construction time**, so an unsafe policy can never be
built or attached (fail-closed) rather than attaching with its guard bypassed.

The floors live here (core), not in any UI. The config write port (BDP-2414,
ADR-0150) reuses these validators so a live edit through ``/v1/config`` is gated
identically to direct construction.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

#: A two-person rule needs at least two distinct approvers — one is not two-key.
MIN_APPROVERS_FLOOR = 2

# Absolute sanity ceilings: a generous backstop against "effectively infinite"
# values until the per-tenant org-max clamp lands with the config write port.
# ponytail: absolute sanity caps; replace with per-tenant org-max in BDP-2414.
COST_CEILING_SANITY_USD = 100_000.0
SPAWN_BREADTH_SANITY = 10_000


class PolicyFloorError(ValueError):
    """A policy factory param violates a non-negotiable safety floor.

    Subclasses :class:`ValueError` so existing ``except ValueError`` attach paths
    treat a floor breach as a construction failure (fail-closed).
    """


def require_int_at_least(name: str, value: object, floor: int) -> int:
    """Return *value* as an int, or raise if it is not an int ``>= floor``.

    ``bool`` is rejected explicitly (it is an ``int`` subclass, so ``True`` would
    otherwise pass as ``1``). Use for a lower-only floor (e.g. min_approvers),
    where a larger value is strictly more restrictive and therefore safe.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyFloorError(f"{name} must be an integer, got {value!r}")
    if value < floor:
        raise PolicyFloorError(f"{name} must be >= {floor} (safety floor), got {value}")
    return value


def require_int_in_range(name: str, value: object, floor: int, ceiling: int) -> int:
    """Return *value* as an int in ``[floor, ceiling]``, or raise.

    Use when *both* directions matter — e.g. a spawn cap, where too high enables
    runaway fan-out and a negative is nonsensical (``0`` = deny-all is allowed).
    """
    require_int_at_least(name, value, floor)
    if value > ceiling:  # type: ignore[operator]  # narrowed to int above
        raise PolicyFloorError(
            f"{name} must be <= {ceiling} (sanity ceiling), got {value}"
        )
    return value  # type: ignore[return-value]


def require_positive_finite(name: str, value: object, ceiling: float) -> float:
    """Return *value* as a finite ``float`` in ``(0, ceiling]``, or raise.

    Guards a budget ceiling: a non-numeric, non-finite (``inf``/``nan``),
    non-positive, or effectively-infinite cap would leave the circuit breaker
    unable to ever trip.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyFloorError(f"{name} must be a number, got {value!r}")
    coerced = float(value)
    if not math.isfinite(coerced) or coerced <= 0:
        raise PolicyFloorError(
            f"{name} must be a finite number > 0, got {value!r}"
        )
    if coerced > ceiling:
        raise PolicyFloorError(
            f"{name} must be <= {ceiling} (sanity ceiling), got {value!r}"
        )
    return coerced


def require_true(name: str, value: object) -> bool:
    """Raise unless *value* is exactly ``True`` — for a legal floor that cannot be off."""
    if value is not True:
        raise PolicyFloorError(
            f"{name} is a legal/safety floor and cannot be disabled (must be true)"
        )
    return True


def require_non_empty(name: str, values: Iterable[object]) -> list:
    """Raise if *values* is empty — an empty gate set silently disables the gate."""
    items = list(values)
    if not items:
        raise PolicyFloorError(
            f"{name} must be non-empty — an empty set disables the gate (fail-open)"
        )
    return items


def reject_wildcard(name: str, values: Iterable[object]) -> list:
    """Raise if *values* contains a wildcard — authority must be enumerated, not '*'."""
    items = list(values)
    if any(v in ("*", "**", ".*") for v in items):
        raise PolicyFloorError(
            f"{name} may not contain a wildcard ('*') — enumerate targets explicitly"
        )
    return items
