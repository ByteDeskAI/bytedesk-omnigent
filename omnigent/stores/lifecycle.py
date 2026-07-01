"""Async store lifecycle hooks — startup / shutdown / health.

Part of the omnigent core-refactor spine (BDP-2327, Phase 2). The store
ABCs (``AgentStore``, ``ConversationStore``, ``FileStore``,
``PolicyStore``, ``PermissionStore``, ``CommentStore``, ``ArtifactStore``,
``HostStore``) have no lifecycle surface today: they are constructed inline
and used immediately, with no place to open a connection pool on boot or
flush it on shutdown, and no uniform liveness probe.

This module adds that surface **without changing any existing store's
behavior**:

- :class:`StoreLifecycleMixin` carries async ``startup()``,
  ``shutdown()``, and ``health_check()`` with **no-op defaults**. A store
  that opts in by inheriting it (or that simply defines the same methods)
  gets real hooks; a store that does not inherit it is wholly untouched —
  exactly like the non-abstract default methods already on
  :class:`~omnigent.stores.agent_store.AgentStore`
  (``set_sot_tier`` / ``get_capabilities`` …).
- :func:`run_store_lifecycle` is a driver: given a set of
  store objects it invokes one lifecycle phase across them, calling a hook
  **only when the store actually defines an awaitable for it**. Stores
  without the hook are skipped, so this is safe to point at the existing
  store set whether or not any store has opted in.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable
from typing import Any, Literal

_logger = logging.getLogger(__name__)

#: The lifecycle phases a store may implement. ``health_check`` is the
#: only phase whose return value is meaningful (``True`` = healthy).
LifecyclePhase = Literal["startup", "shutdown", "health_check"]


class StoreLifecycleMixin:
    """Optional async lifecycle hooks for a persistence store.

    Mixing this into a store ABC (or a concrete store) gives it the three
    hooks below with **no-op defaults**, so existing stores that inherit
    it gain the surface without any behavior change. A backend overrides
    only the hooks it needs (e.g. open/close a connection pool); the rest
    stay inert.

    The hooks are ``async`` so backends that talk to a network resource
    on boot/teardown can ``await`` it. The no-op defaults still return a
    coroutine, so :func:`run_store_lifecycle` can ``await`` them
    uniformly.
    """

    async def startup(self) -> None:
        """Prepare the store for use (e.g. open a connection pool).

        No-op by default. Override in a backend that needs eager setup;
        idempotency is the backend's responsibility.
        """

    async def shutdown(self) -> None:
        """Release the store's resources (e.g. close a pool, flush).

        No-op by default. Override in a backend that holds resources that
        must be released on a clean shutdown.
        """

    async def health_check(self) -> bool:
        """Report whether the store is currently usable.

        :returns: ``True`` when the store is healthy. The no-op default
            returns ``True`` — a store that has not opted into a real
            probe is assumed healthy, matching today's behavior where
            there is no probe at all.
        """
        return True


async def _invoke_one(store: Any, phase: LifecyclePhase) -> bool | None:  # type: ignore[explicit-any]  # heterogeneous store objects
    """Invoke a single lifecycle phase on one store, if it defines it.

    The hook is called only when ``store`` has a callable attribute named
    ``phase`` that returns an awaitable. Stores without the hook are
    skipped (returns ``None``), so this tolerates the existing store set
    where most stores have not opted into :class:`StoreLifecycleMixin`.

    :param store: A persistence store object.
    :param phase: The lifecycle phase to run.
    :returns: The hook's result (``bool`` for ``health_check``), or
        ``None`` when the store does not define the hook.
    """
    hook = getattr(store, phase, None)
    if not callable(hook):
        return None
    result = hook()
    if not inspect.isawaitable(result):
        # Defensively tolerate a sync override; nothing to await.
        return result  # type: ignore[no-any-return]
    return await result


async def run_store_lifecycle(
    stores: Iterable[Any],  # type: ignore[explicit-any]  # heterogeneous store objects
    phase: LifecyclePhase,
) -> dict[int, bool | None]:
    """Drive one lifecycle phase across a set of stores.

    A caller can hand this the existing store set to run ``startup`` /
    ``shutdown`` / ``health_check`` uniformly. Each store's hook is
    invoked **only when present** (see :func:`_invoke_one`), so stores
    that have not opted into :class:`StoreLifecycleMixin` are
    transparently skipped.

    :param stores: The store objects to drive the phase across, in order.
    :param phase: The lifecycle phase to run on each store.
    :returns: Mapping of ``id(store) -> hook result`` for every store
        that defined the hook; absent stores are omitted.
    """
    results: dict[int, bool | None] = {}
    for store in stores:
        outcome = await _invoke_one(store, phase)
        if outcome is None and not _defines_hook(store, phase):
            continue
        results[id(store)] = outcome
        if phase == "health_check" and outcome is False:
            _logger.warning(
                "store %s reported unhealthy on health_check",
                type(store).__name__,
            )
    return results


def _defines_hook(store: Any, phase: LifecyclePhase) -> bool:  # type: ignore[explicit-any]  # heterogeneous store objects
    """Whether ``store`` exposes a callable for ``phase``.

    Used to distinguish "hook absent" from "hook present and returned
    ``None``" (a legitimate ``startup``/``shutdown`` result).

    :param store: A persistence store object.
    :param phase: The lifecycle phase name.
    :returns: ``True`` when ``store`` has a callable attribute ``phase``.
    """
    return callable(getattr(store, phase, None))


__all__ = [
    "LifecyclePhase",
    "StoreLifecycleMixin",
    "run_store_lifecycle",
]
