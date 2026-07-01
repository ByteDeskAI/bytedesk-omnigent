"""ClaudeSDKExecutor harness — thin re-export facade."""
from __future__ import annotations

import importlib

_SUBMODULES = (
    "_constants",
    "_state",
    "_cli",
    "_content",
    "_executor",
    "_mcp",
    "_process",
    "_protocols",
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
from . import _bootstrap

for _key, _value in _bootstrap.__dict__.items():
    if _key.startswith("__") or _key in _FACADE_SKIP:
        continue
    globals()[_key] = _value
