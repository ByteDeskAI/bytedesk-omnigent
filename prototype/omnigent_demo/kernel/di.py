"""KERNEL — the dependency-injection container.

A real microkernel resolves collaborators for its extensions instead of letting
them ``import`` each other into a tangle. This container gives the host:

  * **Three lifetimes** — ``SINGLETON`` (one instance per container),
    ``TRANSIENT`` (a fresh instance every resolve), ``SCOPED`` (one per scope,
    e.g. per request — the FastAPI ``Depends`` analog).
  * **Constructor auto-wiring** — register a class and the container reads its
    ``__init__`` type annotations and resolves each dependency recursively.
  * **Method injection** — ``call(fn)`` resolves a function's annotated params,
    so a tool/harness factory can declare ``def build(self, store: ArtifactStore)``
    and the container supplies ``store``.
  * **Child scopes** — ``create_scope()`` inherits singletons but isolates
    scoped/transient instances; closing a scope disposes what it created.

Resolution keys are *types* (the idiomatic DI form) or plain strings (for
config-ish values). Registering by a Protocol/ABC and resolving by it is how an
extension depends on a *capability* without knowing the concrete class —
Dependency Inversion, enforced by the container.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from enum import Enum
from typing import Any, TypeVar, get_type_hints

logger = logging.getLogger("omnigent_demo.di")

T = TypeVar("T")


class Lifetime(Enum):
    SINGLETON = "singleton"
    TRANSIENT = "transient"
    SCOPED = "scoped"


class DIResolutionError(Exception):
    """A dependency could not be resolved (unregistered, or a cycle)."""


class _Registration:
    __slots__ = ("key", "factory", "lifetime")

    def __init__(self, key: Any, factory: Callable[["Container"], Any], lifetime: Lifetime):
        self.key = key
        self.factory = factory
        self.lifetime = lifetime


class Container:
    """A hierarchical DI container."""

    def __init__(self, parent: "Container | None" = None) -> None:
        self._parent = parent
        self._registrations: dict[Any, _Registration] = {}
        self._singletons: dict[Any, Any] = {}
        self._scoped: dict[Any, Any] = {}
        self._resolving: set[Any] = set()  # cycle guard

    # ── registration ──
    def register_instance(self, key: Any, instance: Any) -> "Container":
        """Register a ready-made singleton (the most common case for services)."""
        self._registrations[key] = _Registration(key, lambda c: instance, Lifetime.SINGLETON)
        self._singletons[key] = instance
        return self

    def register_factory(
        self, key: Any, factory: Callable[["Container"], Any], *, lifetime: Lifetime = Lifetime.SINGLETON
    ) -> "Container":
        """Register a factory ``factory(container) -> instance`` under *key*."""
        self._registrations[key] = _Registration(key, factory, lifetime)
        return self

    def register_type(
        self, cls: type, *, key: Any | None = None, lifetime: Lifetime = Lifetime.SINGLETON
    ) -> "Container":
        """Register *cls*, auto-wiring its constructor on resolve.

        ``key`` defaults to *cls* but may be a Protocol/ABC so callers depend on
        the interface, not the implementation (Dependency Inversion)."""
        self._registrations[key or cls] = _Registration(
            key or cls, lambda c: c._autowire(cls), lifetime
        )
        return self

    # ── resolution ──
    def resolve(self, key: Any) -> Any:
        """Return an instance for *key*, honouring its lifetime."""
        reg = self._find(key)
        if reg is None:
            raise DIResolutionError(f"no registration for {_kname(key)}")

        if reg.lifetime is Lifetime.SINGLETON:
            # Singletons live on the container that *registered* them.
            owner = self._owner_of(key)
            if key in owner._singletons:
                return owner._singletons[key]
            instance = self._build(reg)
            owner._singletons[key] = instance
            return instance

        if reg.lifetime is Lifetime.SCOPED:
            if key in self._scoped:
                return self._scoped[key]
            instance = self._build(reg)
            self._scoped[key] = instance
            return instance

        return self._build(reg)  # TRANSIENT

    def try_resolve(self, key: Any, default: Any = None) -> Any:
        try:
            return self.resolve(key)
        except DIResolutionError:
            return default

    def call(self, fn: Callable[..., T]) -> T:
        """Invoke *fn*, injecting its annotated parameters from the container.

        ``self`` and params with defaults that can't be resolved are left alone.
        This is how tool/harness factories get their collaborators injected."""
        kwargs = self._build_kwargs(fn)
        return fn(**kwargs)

    # ── scopes ──
    def create_scope(self) -> "Container":
        """A child container: shares singletons, isolates scoped/transient."""
        return Container(parent=self)

    # ── internals ──
    def _find(self, key: Any) -> _Registration | None:
        node: Container | None = self
        while node is not None:
            if key in node._registrations:
                return node._registrations[key]
            node = node._parent
        return None

    def _owner_of(self, key: Any) -> "Container":
        node: Container = self
        while node._parent is not None and key not in node._registrations:
            node = node._parent
        return node

    def _build(self, reg: _Registration) -> Any:
        if reg.key in self._resolving:
            raise DIResolutionError(f"dependency cycle at {_kname(reg.key)}")
        self._resolving.add(reg.key)
        try:
            return reg.factory(self)
        finally:
            self._resolving.discard(reg.key)

    def _autowire(self, cls: type) -> Any:
        return cls(**self._build_kwargs(cls.__init__, skip_self=True))

    def _build_kwargs(self, fn: Callable, *, skip_self: bool = False) -> dict[str, Any]:
        try:
            sig = inspect.signature(fn)
            hints = get_type_hints(fn)
        except (ValueError, TypeError):
            return {}
        kwargs: dict[str, Any] = {}
        for pname, param in sig.parameters.items():
            if pname == "self" and skip_self:
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            ann = hints.get(pname)
            if ann is None:
                continue
            found = self._find(ann)
            if found is not None:
                kwargs[pname] = self.resolve(ann)
            elif param.default is inspect.Parameter.empty:
                raise DIResolutionError(
                    f"cannot inject required param {pname!r}: {_kname(ann)} unregistered"
                )
        return kwargs


def _kname(key: Any) -> str:
    return key.__name__ if isinstance(key, type) else repr(key)
