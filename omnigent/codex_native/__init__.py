"""Codex native terminal launcher — thin re-export facade."""
from __future__ import annotations

import importlib

_SUBMODULES = (
    "_constants",
    "_state",
    "_entry",
    "_helpers",
    "_local_server",
    "_remote_server",
    "_resume_ui",
    "_rollout",
    "_session_items",
    "_terminal",
    "_types",
)


_FACADE_SKIP = frozenset({"_SUBMODULES", "_export_submodule", "importlib", "_FACADE_SKIP"})

def _export_submodule(name: str) -> None:
    mod = importlib.import_module(f".{name}", __name__)
    for key, value in mod.__dict__.items():
        if key.startswith("__") or key in _FACADE_SKIP:
            continue
        globals()[key] = value


for _name in _SUBMODULES:
    _export_submodule(_name)
