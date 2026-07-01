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
_IMPORTED_SUBMODULES = []


def _export_submodule(name: str):
    mod = importlib.import_module(f".{name}", __name__)
    for key, value in mod.__dict__.items():
        if key.startswith("__") or key in _FACADE_SKIP:
            continue
        globals()[key] = value
    return mod


for _name in _SUBMODULES:
    _IMPORTED_SUBMODULES.append(_export_submodule(_name))


def _wire_submodule_globals() -> None:
    exports = {
        key: value
        for key, value in globals().items()
        if not key.startswith("__") and key not in _FACADE_SKIP
    }
    modules = list(_IMPORTED_SUBMODULES)
    runner_facade = importlib.import_module("._runner", __name__)
    for runner_name in getattr(runner_facade, "_SUBMODULES", ()):
        modules.append(importlib.import_module(f"._runner.{runner_name}", __name__))
    for mod in modules:
        for key, value in exports.items():
            mod.__dict__.setdefault(key, value)


_wire_submodule_globals()
