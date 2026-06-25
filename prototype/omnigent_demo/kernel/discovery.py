"""KERNEL — extension discovery (entry-points + env-var override).

Faithful to the real ``omnigent.extensions``: extensions self-register by
declaring themselves under the ``omnigent.extensions`` setuptools entry-point
group; the ``OMNIGENT_EXTENSIONS`` env var (comma-separated ``module:factory``)
is an additional source so a source-mounted local-dev checkout loads without the
image's baked ``entry_points.txt``.

The host never hard-codes a list of known extensions — it *discovers* whatever
declared itself. That is what makes "register from inside the extension" work:
the extension's own package metadata is the registration.

For this standalone demo we expose a tiny in-memory entry-point table
(``register_entry_point``) so it runs without `pip install`. In real omnigent
the same function body calls ``importlib.metadata.entry_points(group=...)``.
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Callable

from .protocol import Extension

logger = logging.getLogger("omnigent_demo.discovery")

ENTRY_POINT_GROUP = "omnigent.extensions"
ENV_VAR = "OMNIGENT_EXTENSIONS"

# Demo stand-in for installed entry-point metadata: {name: zero-arg factory}.
# In real omnigent this dict does not exist — entry_points(group=...) is the source.
_DEMO_ENTRY_POINTS: dict[str, Callable[[], Extension]] = {}


def register_entry_point(name: str, factory: Callable[[], Extension]) -> None:
    """Demo-only: simulate a package declaring itself under the entry-point group."""
    _DEMO_ENTRY_POINTS[name] = factory


def _load_from_env() -> list[Extension]:
    """Parse ``OMNIGENT_EXTENSIONS=pkg.mod:Factory,other.mod:Factory``."""
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return []
    out: list[Extension] = []
    for spec in (s.strip() for s in raw.split(",") if s.strip()):
        module_path, _, attr = spec.partition(":")
        try:
            mod = importlib.import_module(module_path)
            factory = getattr(mod, attr)
            out.append(factory())
        except Exception:  # noqa: BLE001 — one bad spec must not break boot
            logger.warning("OMNIGENT_EXTENSIONS: failed to load %r", spec, exc_info=True)
    return out


def discover_extensions() -> list[Extension]:
    """Return every extension that declared itself (entry-points + env var).

    Error-isolated: one extension that fails to instantiate is logged and
    skipped — it must never break the others or boot.
    """
    found: list[Extension] = []
    for name, factory in _DEMO_ENTRY_POINTS.items():
        try:
            found.append(factory())
        except Exception:  # noqa: BLE001
            logger.warning("entry-point %r failed to load", name, exc_info=True)
    found.extend(_load_from_env())
    return found
