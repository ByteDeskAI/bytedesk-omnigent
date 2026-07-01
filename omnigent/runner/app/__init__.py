"""Runner FastAPI app — thin re-export facade."""
from __future__ import annotations

import importlib

_SUBMODULES = (
    "_constants",
    "_state",
    "_dispatch",
    "_forwarders",
    "_harness",
    "_helpers",
    "_policy",
    "_streaming",
    "_subagents",
    "_terminals",
    "_timers",
    "_tools",
)

from .factory import create_runner_app, create_runner_app_from_env


_FACADE_SKIP = frozenset({"_SUBMODULES", "_export_submodule", "importlib", "_FACADE_SKIP"})


def _export_submodule(name: str) -> None:
    mod = importlib.import_module(f".{name}", __name__)
    for key, value in mod.__dict__.items():
        if key.startswith("__") or key in _FACADE_SKIP:
            continue
        globals()[key] = value


for _name in _SUBMODULES:
    _export_submodule(_name)
