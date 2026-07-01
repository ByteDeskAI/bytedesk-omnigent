"""CLI entry point for omnigent."""

from __future__ import annotations

import collections.abc
import contextlib
import copy
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeAlias, cast

import click
import yaml
from pydantic import BaseModel, ConfigDict
from rich import box
from rich.console import Console
from rich.table import Table

from omnigent._startup_profile import StartupProfiler
from omnigent.cli_sandbox import lakebox as _lakebox_alias_group
from omnigent.cli_sandbox import sandbox as _sandbox_group
from omnigent.harness_aliases import canonicalize_harness
from omnigent.host.local_server import (
    _DEFAULT_LOCAL_PORT,
    _pid_alive,
    ensure_local_omnigent_server,
    local_server_status,
    local_server_url_if_healthy,
    server_config_signature,
    stop_local_omnigent_server,
    stop_untracked_local_server,
)
from omnigent.onboarding.sandboxes import available_providers as _sandbox_providers
from omnigent.onboarding.ucode_setup import (
    build_ucode_configure_command,
    find_ucode_command,
    model_gateway_workspace_urls,
)

if TYPE_CHECKING:
    import httpx

    from omnigent._runner_startup import RunnerStartupProgress
    from omnigent.onboarding.ambient import DetectedProvider
    from omnigent.onboarding.provider_config import ProviderEntry


# Any: YAML configs have heterogeneous value types (str, int, list, etc.)
def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _format_version() -> str:
    """Render the version line shown by ``--version`` and ``version``.

    Always includes the package version. When the build hook in
    ``setup.py`` wrote ``omnigent/_build_info.py``, the line is
    additionally annotated with the short commit SHA and the build
    time in ISO-8601 UTC. For source checkouts that have never
    been built, only the bare version prints — matching the
    behavior before this feature shipped.

    :returns: Either ``"omnigent 0.1.0"`` (no build info), or
        ``"omnigent 0.1.0 (010cf77c, built 2026-05-21T14:34:45Z)"``.
    """
    import datetime
    import importlib.metadata

    from omnigent.update_check import _read_build_info

    version_str = importlib.metadata.version("omnigent")
    info = _read_build_info()
    if info is None:
        return f"omnigent {version_str}"
    epoch, sha = info
    when = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if sha:
        # Short SHA (first 8 chars) — enough to disambiguate in bug
        # reports without making the line unwieldy.
        return f"omnigent {version_str} ({sha[:8]}, built {when})"
    # _build_info exists but has no SHA (built without git available).
    return f"omnigent {version_str} (built {when})"

def _print_version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Click callback that lazily renders the version line and exits.

    We deliberately do NOT use ``@click.version_option(version=...)``
    here: that decorator evaluates its ``version`` argument at module
    import time, which would call ``_format_version()`` — and through
    it ``_read_build_info()`` — during ``omnigent.cli`` import. The
    successful sub-import would then set ``omnigent._build_info`` as
    an attribute on the ``omnigent`` package object. Once that
    attribute exists, ``from omnigent import _build_info`` short-
    circuits *before* consulting ``sys.modules``, defeating the
    test-suite's ``sys.modules[...] = None`` blocker and making most
    update_check tests pick up live values from disk.

    Doing the work in a callback keeps the import side-effect-free:
    ``_format_version`` runs only when the user actually passes
    ``--version`` on the command line.
    """
    if not value or ctx.resilient_parsing:
        return
    click.echo(_format_version())
    ctx.exit()

def _should_skip_update_check(argv: list[str]) -> bool:
    """Decide whether the update notice should be suppressed for *argv*.

    Skipped for help / version requests, internal TUI subcommands
    (``pane-split`` / ``pane-picker``, invoked by the terminal UI rather
    than the user), and ``upgrade`` itself (pointing the user at
    ``omni upgrade`` while they are running it is noise).

    :param argv: CLI arguments without the program name, e.g.
        ``["run", "agent.yaml"]``.
    :returns: ``True`` when the update notice should not be shown.
    """
    if not argv:
        return True
    return argv[0] in {
        "--help",
        "-h",
        "--version",
        "version",
        "upgrade",
        "pane-split",
        "pane-picker",
    }

