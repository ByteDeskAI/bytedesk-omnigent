"""Generic registry for a pluggable seam (BDP-2345).

A :class:`PluggableRegistry` turns an ad-hoc ``if scheme == ... else ...`` selection
into the canonical 4-invariant pluggable recipe (see the package docstring):

1. **Protocol per seam** — the registry is generic over ``T`` (the provider type).
2. **Registry + default fallback** — :meth:`register` adds named factories, a
   registered default is the active provider unless overridden.
3. **Entry-point discovery** — :meth:`discover_extensions` consults a per-seam hook
   on each discovered :class:`~omnigent.extensions.OmnigentExtension`, error-isolated
   exactly like :func:`omnigent.extensions.extension_secret_backends`.
4. **Optional strangler flag** — ``OMNIGENT_USE_<SEAM>`` picks the active impl by
   name at resolve time (default = the registered default), so a new backend ships
   dark and flips on per-env without a code change.

Factories are stored, not instances: :meth:`get` / :meth:`resolve_default` call the
factory each time so optional dependencies are only imported on selection (the same
deferral the artifact-store if/else relied on).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Generic, TypeVar

from omnigent.kernel.pluggable.errors import ProviderNotRegistered, RegistryConflict

logger = logging.getLogger(__name__)


def discover_extensions():
    """Lazy proxy to :func:`omnigent.extensions.discover_extensions`.

    ``omnigent.extensions`` is the server-side extension-discovery hub and is
    heavyweight (its router types pull the FastAPI stack). Importing a
    ``PluggableRegistry`` must NOT drag that onto the runner hot path
    (e.g. ``omnigent.runner.identity``, which re-execs per spawn), so the
    import is deferred to the moment discovery actually runs — which only
    happens server-side. Kept as a module-level symbol so tests can patch it.
    """
    from omnigent.kernel.extensions import discover_extensions as _discover

    return _discover()

T = TypeVar("T")


def _override_env_name(seam: str) -> str:
    """Env var that pins the active provider for *seam* (``OMNIGENT_USE_<SEAM>``)."""
    return f"OMNIGENT_USE_{seam.upper()}"


class PluggableRegistry(Generic[T]):
    """A named-factory registry for one pluggable seam."""

    def __init__(
        self, seam: str, *, default: tuple[str, Callable[[], T]] | None = None
    ) -> None:
        """Create a registry for *seam*, optionally registering a default impl.

        :param seam: Stable seam identifier (e.g. ``"artifact_store"``); also the
            suffix of the ``OMNIGENT_USE_<SEAM>`` override env var.
        :param default: ``(name, factory)`` registered immediately and used as the
            active provider when no override env is set.
        """
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
        """Register *factory* under *name*.

        :raises RegistryConflict: if *name* is already registered for this seam.
        """
        if name in self._factories:
            raise RegistryConflict(self._seam, name)
        self._factories[name] = factory

    def get(self, name: str) -> T:
        """Resolve and return the provider registered under *name*.

        :raises ProviderNotRegistered: if no provider is registered under *name*.
        """
        try:
            factory = self._factories[name]
        except KeyError:
            raise ProviderNotRegistered(
                self._seam, name, known=self._factories.keys()
            ) from None
        return factory()

    def _active_name(self) -> str:
        """The name of the provider that :meth:`resolve_default` will resolve.

        Reads ``OMNIGENT_USE_<SEAM>`` (an empty/whitespace value is ignored);
        falls back to the registered default name.

        :raises ProviderNotRegistered: if no default and no override is set.
        """
        # Imported here so the env read is testable via monkeypatch of os.environ
        # and so the dependency surface stays minimal.
        import os

        override = os.environ.get(_override_env_name(self._seam), "").strip()
        if override:
            return override
        if self._default_name is None:
            raise ProviderNotRegistered(
                self._seam, "<default>", known=self._factories.keys()
            )
        return self._default_name

    def resolve_default(self) -> T:
        """Resolve the active provider (override env, else registered default).

        :raises ProviderNotRegistered: if the override names an unknown provider,
            or no default was registered and no override is set.
        """
        return self.get(self._active_name())

    def discover_extensions(self, *, hook: str) -> None:
        """Register providers contributed by extensions via the per-seam *hook*.

        Walks :func:`omnigent.extensions.discover_extensions` and, for each
        extension defining a *hook* method, merges its ``{name: factory}`` mapping.
        Mirrors :func:`omnigent.extensions.extension_secret_backends`: a single bad
        extension is logged and skipped — it must never break the others or boot.

        :param hook: Method name on the extension returning ``{name: factory}``
            (e.g. ``"artifact_store_providers"``).
        """
        for ext in discover_extensions():
            getter = getattr(ext, hook, None)
            if getter is None:
                continue
            try:
                contributed = getter()
                for name, factory in dict(contributed).items():
                    self.register(name, factory)
            except Exception:  # noqa: BLE001 — extensions are best-effort
                logger.warning(
                    "extension %r failed to contribute %s providers for seam %r",
                    getattr(ext, "name", ext),
                    hook,
                    self._seam,
                    exc_info=True,
                )

    def names(self) -> list[str]:
        """The registered provider names."""
        return list(self._factories)

    def describe(self) -> dict:
        """A capability-manifest view: seam, registered names, active, default."""
        try:
            active = self._active_name()
        except ProviderNotRegistered:
            active = None
        return {
            "seam": self._seam,
            "names": self.names(),
            "active": active,
            "default": self._default_name,
        }


__all__ = ["PluggableRegistry", "_override_env_name"]
