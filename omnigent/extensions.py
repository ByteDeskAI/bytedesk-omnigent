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

import logging
from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable

from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)

#: setuptools entry-point group extensions register under.
ENTRY_POINT_GROUP = "omnigent.extensions"


@runtime_checkable
class OmnigentExtension(Protocol):
    """An out-of-core extension the server discovers and installs (ADR-0143)."""

    name: str

    def routers(self) -> list[APIRouter]:
        """FastAPI routers to mount under ``/v1`` (empty list if none)."""
        ...


def discover_extensions() -> list[OmnigentExtension]:
    """Load every extension registered under :data:`ENTRY_POINT_GROUP`.

    A single bad extension is logged and skipped — it must never break server boot.
    """
    found: list[OmnigentExtension] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            found.append(ep.load()())
        except Exception:  # one bad extension must not break server boot
            logger.exception("failed to load omnigent extension %r", ep.name)
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
