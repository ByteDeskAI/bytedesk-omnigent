"""Typed service registry — composition-root container for app services.

Part of the omnigent core-refactor spine (BDP-2327, Phase 1). Today
:func:`omnigent.server.create_app` scatters its singleton services across
``app.state.<name> = ...`` attribute writes (the tunnel registry, runner
router, host registry, server metrics, …). ``app.state`` is an untyped
``SimpleNamespace``: a typo in an attribute name fails only at read time,
and there is no single place that enumerates what a built app exposes.

:class:`ServiceRegistry` is a thin, typed wrapper over a ``dict`` keyed by
the service's type, with explicit ``register`` / ``get`` accessors. It is
introduced behind ``OMNIGENT_USE_SERVICE_REGISTRY`` (default OFF) as a
**dual-write** sidecar: ``create_app`` keeps every existing
``app.state.x = ...`` write and, only when the flag is on, mirrors the
same object into a registry. Nothing reads from the registry yet — when
the flag is off the registry is never constructed and the app behaves
byte-identically to today. Later phases migrate readers off ``app.state``
onto :meth:`get`; this phase only stands the container up.

This module deliberately holds no omnigent service imports — services are
registered by type at the call site, so the registry stays a generic,
upstream-friendly container with no dependency on the rest of the app.
"""

from __future__ import annotations

from typing import TypeVar

_T = TypeVar("_T")


class ServiceRegistry:
    """A typed registry of singleton services keyed by their type.

    Each service is stored under its concrete ``type`` so retrieval is
    type-driven (``registry.get(RunnerRouter)``) rather than stringly
    keyed. Registering a second instance of the same type replaces the
    first — the composition root wires each service exactly once, so a
    duplicate registration is a wiring bug the caller wants to overwrite
    deterministically rather than silently accumulate.

    The registry is intentionally minimal: it is a container, not a
    service locator with lifecycle. Construction and teardown stay in
    the composition root (``create_app`` + its lifespan); this only
    records the wired instances so a single typed surface can enumerate
    them.
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._services: dict[type, object] = {}

    def register(self, service: _T, *, as_type: type[_T] | None = None) -> _T:
        """Register *service* under its type and return it.

        :param service: The service instance to register, e.g. a
            ``RunnerRouter``.
        :param as_type: Optional explicit key type to register under,
            for when the desired lookup type differs from the runtime
            type (e.g. registering a concrete implementation under its
            Protocol/ABC). Defaults to ``type(service)``.
        :returns: The same ``service`` instance, so the call can wrap an
            inline construction (``router = registry.register(RunnerRouter(...))``).
        """
        key = as_type if as_type is not None else type(service)
        self._services[key] = service
        return service

    def get(self, service_type: type[_T]) -> _T:
        """Return the registered instance of *service_type*.

        :param service_type: The type the service was registered under.
        :returns: The registered service instance.
        :raises KeyError: If no service of that type was registered.
        """
        try:
            service = self._services[service_type]
        except KeyError as exc:
            raise KeyError(
                f"no service registered for {service_type.__name__!r}"
            ) from exc
        return service  # type: ignore[return-value]  # keyed by its own type

    def try_get(self, service_type: type[_T]) -> _T | None:
        """Return the registered instance of *service_type*, or ``None``.

        The optional-lookup variant of :meth:`get` for services that may
        not be wired in every deployment (e.g. a ``HostStore`` is absent
        when host support is disabled).

        :param service_type: The type the service was registered under.
        :returns: The registered service, or ``None`` when absent.
        """
        return self._services.get(service_type)  # type: ignore[return-value]

    def __contains__(self, service_type: type) -> bool:
        """Return whether a service of *service_type* is registered.

        :param service_type: The type to check for.
        :returns: ``True`` when an instance is registered.
        """
        return service_type in self._services


__all__ = ["ServiceRegistry"]
