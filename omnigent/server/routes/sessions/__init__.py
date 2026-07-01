"""Routes for the Sessions API (``/v1/sessions``) — thin re-export facade."""
from __future__ import annotations

import importlib

_SUBMODULES = (
    "_constants",
    "_state",
    "_access",
    "_create",
    "_elicitation",
    "_external_events",
    "_helpers",
    "_list_updates",
    "_managed_launch",
    "_mcp",
    "_native",
    "_policy",
    "_publish",
    "_resources",
    "_runner",
    "_skills",
    "_snapshot",
    "_subagent",
    "_usage",
)

from .router import create_sessions_router


_FACADE_SKIP = frozenset({"_SUBMODULES", "_export_submodule", "importlib", "_FACADE_SKIP"})


def _export_submodule(name: str) -> None:
    mod = importlib.import_module(f".{name}", __name__)
    for key, value in mod.__dict__.items():
        if key.startswith("__") or key in _FACADE_SKIP:
            continue
        globals()[key] = value


for _name in _SUBMODULES:
    _export_submodule(_name)
