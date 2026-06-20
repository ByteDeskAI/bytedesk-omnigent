"""Generic first-party extension seam (ADR-0143).

The single, generic mechanism that lets out-of-core packages add functionality to
the omnigent server without editing the core app factory: extensions are
discovered via the ``omnigent.extensions`` setuptools entry-point group and each
one's routers are mounted. This module is deliberately **generic** (no reference
to any specific extension) and is the one upstream-contributable seam; all
first-party feature code lives in its own package (e.g. ``bytedesk_omnigent``).

``create_app`` calls :func:`install_extensions` once. The discovery vs. install
split keeps the install logic unit-testable with injected fakes (no installed
entry-point metadata required).
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Awaitable, Callable
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Annotation-only (deferred by `from __future__ import annotations`).
    # Kept out of the runtime import graph so importing this discovery hub —
    # and therefore any PluggableRegistry that discovers extensions — does NOT
    # drag in the ~100ms FastAPI stack on the runner hot path.
    from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)

#: setuptools entry-point group extensions register under.
ENTRY_POINT_GROUP = "omnigent.extensions"
#: Explicit ``module:factory`` registrations (comma-separated), checked in addition
#: to entry-points. Lets source-mounted local-dev pods load extensions without the
#: image's baked entry_points.txt being regenerated (ADR-0143 / BDP-2294).
ENV_VAR = "OMNIGENT_EXTENSIONS"


@runtime_checkable
class OmnigentExtension(Protocol):
    """An out-of-core extension the server discovers and installs (ADR-0143).

    ``name`` and ``routers`` are required. The four capability methods below
    (``tool_factories`` / ``policy_modules`` / ``secret_backends`` /
    ``background_tasks``) are **optional** — an extension contributes only the
    surfaces it has. They are declared here for the type checker; the
    aggregators use ``hasattr`` so an extension that omits one is simply
    skipped (no ``getattr`` default probe).
    """

    name: str

    def routers(self, auth_provider: object | None = ...) -> list[APIRouter]:
        """FastAPI routers to mount under ``/v1`` (empty list if none).

        ``auth_provider`` is passed by :func:`install_extensions` for routes
        that need it; an extension whose ``routers`` takes no argument is still
        accepted (back-compat, handled by the install-time ``TypeError`` retry).
        """
        ...

    # ── optional capability methods ──────────────────────────────────
    def tool_factories(self) -> dict[str, Callable[[object], object]]:
        """Builtin tool factories (``{name: factory(config) -> Tool}``)."""
        ...

    def policy_modules(self) -> list[str]:
        """Policy-builtin module import paths the policy registry scans."""
        ...

    def secret_backends(self) -> list[object]:
        """Secret backends consulted by :mod:`omnigent.onboarding.secrets`."""
        ...

    def background_tasks(self) -> list[Callable[[], Awaitable[None]]]:
        """Background-task factories the server lifespan starts and cancels."""
        ...


def _load_env_extensions() -> list[OmnigentExtension]:
    """Load extensions named in the ``OMNIGENT_EXTENSIONS`` env var (``module:factory``)."""
    found: list[OmnigentExtension] = []
    for entry in os.environ.get(ENV_VAR, "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            module_path, _, attr = entry.partition(":")
            factory = getattr(importlib.import_module(module_path), attr)
            found.append(factory())
        except Exception:  # one bad entry must not break server boot
            logger.exception("failed to load %s entry %r", ENV_VAR, entry)
    return found


def discover_extensions() -> list[OmnigentExtension]:
    """Load extensions from the entry-point group + the ``OMNIGENT_EXTENSIONS`` env.

    Entry-points are the production path (registered at install time); the env var
    is the explicit/local-dev path (no installed metadata needed, BDP-2294).
    Deduped by ``name`` so a redundant env entry doesn't double-register. A single
    bad extension is logged and skipped — it must never break server boot.
    """
    found: list[OmnigentExtension] = []
    seen: set[str] = set()
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            ext = ep.load()()
        except Exception:  # one bad extension must not break server boot
            logger.exception("failed to load omnigent extension %r", ep.name)
            continue
        found.append(ext)
        seen.add(ext.name)
    for ext in _load_env_extensions():
        if ext.name not in seen:
            found.append(ext)
            seen.add(ext.name)
    return found


def install_extensions(
    app: FastAPI,
    *,
    extensions: list[OmnigentExtension] | None = None,
    auth_provider: object | None = None,
) -> list[str]:
    """Mount each extension's routers under ``/v1``; return the installed names.

    :param extensions: defaults to :func:`discover_extensions` (entry-point
        discovery); tests inject fakes directly.
    :param auth_provider: passed to ``ext.routers(auth_provider=...)`` for
        extensions whose routes need it; extensions whose ``routers()`` takes no
        argument are called without it (back-compat).
    """
    exts = discover_extensions() if extensions is None else extensions
    installed: list[str] = []
    for ext in exts:
        try:
            routers = ext.routers(auth_provider=auth_provider)
        except TypeError:
            routers = ext.routers()
        for router in routers:
            app.include_router(router, prefix="/v1")
        installed.append(ext.name)
        logger.info("installed omnigent extension %r", ext.name)
    return installed


def extension_tool_factories() -> dict:
    """Builtin tool factories contributed by extensions (``tool_factories()``).

    Merged into the core ``_BUILTIN_REGISTRY`` so first-party tools register
    without a ByteDesk-specific edit in ``omnigent/tools/builtins/__init__``.
    """
    factories: dict = {}
    for ext in discover_extensions():
        if hasattr(ext, "tool_factories"):
            factories.update(ext.tool_factories())
    return factories


def extension_policy_modules() -> list[str]:
    """Policy-builtin module paths contributed by extensions (``policy_modules()``)."""
    modules: list[str] = []
    for ext in discover_extensions():
        if hasattr(ext, "policy_modules"):
            modules.extend(ext.policy_modules())
    return modules


def extension_secret_backends() -> list:
    """Secret backends contributed by extensions (``secret_backends()``).

    Consulted by :mod:`omnigent.onboarding.secrets` to let an out-of-core package
    (e.g. ``bytedesk_omnigent``'s Infisical backend) take precedence over the
    local keyring/file store while staying upstream-generic here.
    """
    backends: list = []
    for ext in discover_extensions():
        if hasattr(ext, "secret_backends"):
            backends.extend(ext.secret_backends())
    return backends


def extension_background_factories() -> list:
    """Background-task factories contributed by extensions (``background_tasks()``).

    The server lifespan starts each as a task and cancels it on shutdown.
    """
    factories: list = []
    for ext in discover_extensions():
        if hasattr(ext, "background_tasks"):
            factories.extend(ext.background_tasks())
    return factories
