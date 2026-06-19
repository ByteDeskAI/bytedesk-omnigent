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
from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable

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
    """An out-of-core extension the server discovers and installs (ADR-0143)."""

    name: str

    def routers(self) -> list[APIRouter]:
        """FastAPI routers to mount under ``/v1`` (empty list if none)."""
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
    app: FastAPI, *, extensions: list[OmnigentExtension] | None = None
) -> list[str]:
    """Mount each extension's routers under ``/v1``; return the installed names.

    :param extensions: defaults to :func:`discover_extensions` (entry-point
        discovery); tests inject fakes directly.
    """
    exts = discover_extensions() if extensions is None else extensions
    installed: list[str] = []
    for ext in exts:
        for router in ext.routers():
            app.include_router(router, prefix="/v1")
        installed.append(ext.name)
        logger.info("installed omnigent extension %r", ext.name)
    return installed
