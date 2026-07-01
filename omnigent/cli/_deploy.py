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

def _bundled_example_path(name: str) -> str:
    """Return the filesystem path to a bundled example agent directory.

    Located via the packaged ``omnigent.resources.examples`` (symlinks to
    ``examples/<name>`` in a dev checkout, real directories in an installed
    wheel), mirroring how the model catalog is located.

    :param name: Bundled example directory name, e.g. ``"polly"``.
    :returns: Absolute path string to the agent directory.
    """
    import importlib.resources

    return str(importlib.resources.files("omnigent.resources.examples").joinpath(name))

def _materialize_bundled_example(name: str) -> Path:
    """
    Copy a single bundled example YAML into the user config dir.

    ``uv tool install`` installs package files, not the repository checkout, so the
    top-level ``examples/<name>`` paths are not available to users. Materialize a
    user-editable copy under ``~/.omnigent/agents`` and never overwrite an
    existing file so local edits survive reinstalls and reruns.

    :param name: Filename of the bundled example (e.g.
        ``"databricks_coding_agent.yaml"``).
    :returns: Absolute path to the materialized agent YAML.
    """
    agent_path = _GLOBAL_AGENTS_DIR / name
    if agent_path.exists():
        return agent_path

    agent_path.parent.mkdir(parents=True, exist_ok=True)
    resource = resources.files("omnigent.resources.examples").joinpath(name)
    text = resource.read_text(encoding="utf-8")
    executable_placeholder = "__OMNIGENT_PYTHON_EXECUTABLE__"
    text = text.replace('"${OMNIGENT_HOME:-$PWD}/.venv/bin/python"', executable_placeholder)
    text = text.replace("${OMNIGENT_HOME:-$PWD}/.venv/bin/python", executable_placeholder)
    text = text.replace(".venv/bin/python", sys.executable)
    text = text.replace(executable_placeholder, sys.executable)
    agent_path.write_text(text, encoding="utf-8")
    return agent_path

def _materialize_internal_beta_agents() -> Path:
    """
    Materialize every bundled internal-beta example and return the default's path.

    :returns: Absolute path to the default agent YAML
        (:data:`_INTERNAL_BETA_DEFAULT_AGENT_NAME`).
    """
    default_path: Path | None = None
    for name in _INTERNAL_BETA_BUNDLED_AGENTS:
        path = _materialize_bundled_example(name)
        if name == _INTERNAL_BETA_DEFAULT_AGENT_NAME:
            default_path = path
    assert default_path is not None, (
        f"_INTERNAL_BETA_BUNDLED_AGENTS must include {_INTERNAL_BETA_DEFAULT_AGENT_NAME}"
    )
    return default_path

