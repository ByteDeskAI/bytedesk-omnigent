"""CLI entry point for omnigent — thin re-export facade."""
from __future__ import annotations

import importlib

from . import _core as _core_module
from ._core import cli, _is_removed_ad_hoc_invocation, _is_run_shorthand, _is_server_url
from . import commands  # noqa: F401 — register click commands

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


_FACADE_SKIP = frozenset(
    {
        "_SUBMODULES",
        "_export_submodule",
        "importlib",
        "_core_module",
        "_FACADE_SKIP",
        "cli",
        "main",
        "_is_removed_ad_hoc_invocation",
        "_is_run_shorthand",
        "_is_server_url",
    }
)


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


def main() -> None:
    """Console-script entry point exported by the facade."""
    original_cli = _core_module.cli
    _core_module.cli = cli
    try:
        _core_module.main()
    finally:
        _core_module.cli = original_cli
