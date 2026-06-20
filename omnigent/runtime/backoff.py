"""Backoff policy — one registered exp-full-jitter curve (BDP-2361, P9).

Two call sites computed the same exponential-backoff-with-full-jitter curve by
hand: :func:`omnigent.runtime.llm_retry.compute_backoff_delay` and
:meth:`omnigent.spec.types.RetryPolicy.compute_backoff_delay`. They differed only
in indexing convention and whether a server ``retry_after`` hint and a jitter
flag applied — the underlying math (``min(base * 2**exp, max)`` then optional
``* uniform(0.5, 1.5)``) was identical.

This module collapses that into a single :class:`BackoffPolicy` Strategy with one
registered default (:class:`ExpFullJitterBackoff`), swappable via the
``backoff_policy`` :class:`~omnigent.pluggable.PluggableRegistry`. Both call sites
delegate here, so the curve lives in one place and can be replaced (e.g. decorrelated
jitter) without touching the callers.

Behavior is byte-identical to the prior inlined curves: same exponent, same cap,
same ``random.uniform(0.5, 1.5)`` full-jitter multiplier, same ``retry_after``
floor (``max(delay, retry_after)`` applied before the cap).
"""

from __future__ import annotations

import random
from typing import Protocol, runtime_checkable

from omnigent.pluggable import PluggableRegistry


@runtime_checkable
class BackoffPolicy(Protocol):
    """Compute a retry delay from a 0-based attempt exponent."""

    def compute_delay(
        self,
        exponent: int,
        base_s: float,
        max_s: float,
        *,
        retry_after_s: float | None = None,
        jitter: bool = True,
    ) -> float:
        """Return the delay before a retry attempt.

        :param exponent: 0-based exponent — ``0`` is the first retry. Callers
            with a 1-based index pass ``index - 1``.
        :param base_s: Exponential base in seconds.
        :param max_s: Per-retry cap in seconds.
        :param retry_after_s: Server-requested retry floor, applied before the
            cap. ``None`` means no hint.
        :param jitter: Apply the full-jitter multiplier when ``True``.
        :returns: Delay in seconds.
        """
        ...


class ExpFullJitterBackoff:
    """Exponential backoff with full jitter — the historical default curve."""

    def compute_delay(
        self,
        exponent: int,
        base_s: float,
        max_s: float,
        *,
        retry_after_s: float | None = None,
        jitter: bool = True,
    ) -> float:
        """``min(max(base * 2**exp, retry_after), max)`` then ``* uniform(0.5, 1.5)``."""
        delay: float = base_s * float(2**exponent)
        if retry_after_s is not None:
            delay = max(delay, retry_after_s)
        delay = min(delay, max_s)
        if jitter:
            # ``random.uniform`` is typed as ``Any``-returning by the stdlib
            # stub; cast to float so the declared return type holds.
            delay = float(delay * random.uniform(0.5, 1.5))
        return delay


def backoff_policy_registry() -> PluggableRegistry[BackoffPolicy]:
    """Build the ``backoff_policy`` seam registry with the exp-full-jitter default.

    Extensions may contribute alternative curves (e.g. decorrelated jitter) via a
    ``backoff_policy_providers`` hook at a server-side composition root —
    discovery is intentionally *not* run here so importing this module (used on
    the runner retry path) stays off the fastapi-heavy path.

    :returns: A registry whose default is :class:`ExpFullJitterBackoff`.
    """
    return PluggableRegistry(
        "backoff_policy", default=("exp_full_jitter", ExpFullJitterBackoff)
    )


def default_backoff_policy() -> BackoffPolicy:
    """Resolve the active backoff policy (default = exp-full-jitter).

    Honors the ``OMNIGENT_USE_BACKOFF_POLICY`` override env when an extension has
    registered an alternative; otherwise the in-module default.
    """
    return backoff_policy_registry().resolve_default()


__all__ = [
    "BackoffPolicy",
    "ExpFullJitterBackoff",
    "backoff_policy_registry",
    "default_backoff_policy",
]
