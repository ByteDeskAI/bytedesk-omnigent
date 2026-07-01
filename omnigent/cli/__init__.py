"""CLI entry point for omnigent — thin re-export facade."""
from __future__ import annotations

import importlib

_SUBMODULES = (
    "_constants",
    "_state",
    "_config",
    "_daemon",
    "_deploy",
    "_first_run",
    "_helpers",
    "_host_ui",
    "_pane",
    "_runner_proc",
    "_server",
    "_version",
)

from ._core import cli, main
from . import commands  # noqa: F401 — register click commands


_FACADE_SKIP = frozenset({"_SUBMODULES", "_export_submodule", "importlib", "_FACADE_SKIP"})


def _export_submodule(name: str) -> None:
    mod = importlib.import_module(f".{name}", __name__)
    for key, value in mod.__dict__.items():
        if key.startswith("__") or key in _FACADE_SKIP:
            continue
        globals()[key] = value


for _name in _SUBMODULES:
    _export_submodule(_name)

_COMMAND_MODULES = (
    "attach",
    "claude",
    "codex",
    "config",
    "debby",
    "debug",
    "host",
    "login",
    "pane_picker",
    "pane_split",
    "pi",
    "polly",
    "resume",
    "run",
    "server",
    "setup",
    "stop",
    "upgrade",
    "version",
)


def _export_command_module(name: str) -> None:
    mod = importlib.import_module(f".commands.{name}", __name__)
    for key, value in mod.__dict__.items():
        if key.startswith("__") or key in _FACADE_SKIP:
            continue
        globals()[key] = value


for _name in _COMMAND_MODULES:
    _export_command_module(_name)
