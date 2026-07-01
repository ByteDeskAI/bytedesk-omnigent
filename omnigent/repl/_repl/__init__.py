"""Rich-based REPL for omnigent — thin re-export facade."""
from __future__ import annotations

import importlib

_SUBMODULES = (
    "_constants",
    "_state",
    "_adapter",
    "_approval",
    "_commands",
    "_context",
    "_entry",
    "_helpers",
    "_model",
    "_overview",
    "_render",
    "_startup",
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
from . import _bootstrap

for _key, _value in _bootstrap.__dict__.items():
    if _key.startswith("__") or _key in _FACADE_SKIP:
        continue
    globals()[_key] = _value
