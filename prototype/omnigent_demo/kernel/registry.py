"""KERNEL — the generic pluggable-seam registry.

A slim, faithful re-implementation of the real
``omnigent.pluggable.registry.PluggableRegistry`` (BDP-2345). One instance per
seam (``tools``, ``harnesses``, ``policies`` …). It stores *factories*, not
instances, so an optional dependency is only imported when its provider is
actually selected — the same deferral the real registry relies on.

The 4-invariant recipe, unchanged from the real code:
  1. Protocol per seam — generic over ``T``.
  2. Registry + default fallback.
  3. Discovery hook — providers contributed by extensions.
  4. Strangler flag — ``OMNIGENT_USE_<SEAM>`` picks the active impl at resolve time.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class RegistryConflict(Exception):
    """A name was registered twice for the same seam."""

    def __init__(self, seam: str, name: str) -> None:
        super().__init__(f"seam {seam!r}: provider {name!r} already registered")


class ProviderNotRegistered(Exception):
    """A name was requested that no provider was registered under."""

    def __init__(self, seam: str, name: str, known) -> None:
        super().__init__(
            f"seam {seam!r}: no provider {name!r} (known: {sorted(known)})"
        )


class PluggableRegistry(Generic[T]):
    """A named-factory registry for one pluggable seam."""

    def __init__(
        self, seam: str, *, default: tuple[str, Callable[[], T]] | None = None
    ) -> None:
        self._seam = seam
        self._factories: dict[str, Callable[[], T]] = {}
        self._default_name: str | None = None
        if default is not None:
            name, factory = default
            self.register(name, factory)
            self._default_name = name

    @property
    def seam(self) -> str:
        return self._seam

    def register(self, name: str, factory: Callable[[], T]) -> None:
        """Register *factory* under *name*. Raises on a duplicate name."""
        if name in self._factories:
            raise RegistryConflict(self._seam, name)
        self._factories[name] = factory

    def get(self, name: str) -> T:
        """Resolve and return the provider registered under *name*."""
        try:
            factory = self._factories[name]
        except KeyError:
            raise ProviderNotRegistered(
                self._seam, name, known=self._factories.keys()
            ) from None
        return factory()

    def _active_name(self) -> str:
        """Name :meth:`resolve_default` resolves: ``OMNIGENT_USE_<SEAM>`` else default."""
        override = os.environ.get(f"OMNIGENT_USE_{self._seam.upper()}", "").strip()
        if override:
            return override
        if self._default_name is None:
            raise ProviderNotRegistered(
                self._seam, "<default>", known=self._factories.keys()
            )
        return self._default_name

    def resolve_default(self) -> T:
        """Resolve the active provider (strangler override env, else default)."""
        return self.get(self._active_name())

    def names(self) -> list[str]:
        return list(self._factories)

    def items(self) -> dict[str, Callable[[], T]]:
        return dict(self._factories)
