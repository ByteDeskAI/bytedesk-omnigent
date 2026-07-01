"""Sessions runner relay/heal/launch helpers — thin re-export facade."""
from __future__ import annotations

import importlib

_SUBMODULES = (
    "_bundled",
    "_client",
    "_heal",
    "_helpers",
    "_keepalive",
    "_launch",
    "_native",
    "_relay",
    "_resources",
    "_skills",
    "_stop",
)



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
    for mod in _IMPORTED_SUBMODULES:
        for key, value in exports.items():
            mod.__dict__.setdefault(key, value)


_wire_submodule_globals()
