"""Pydantic models for the API layer and SSE stream events — thin re-export facade."""
from __future__ import annotations

import importlib

_SUBMODULES = (
    "_api",
    "_sse",
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
