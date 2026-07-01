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

def _strip_resume_flags(argv: list[str]) -> list[str]:
    """
    Return *argv* with all resume-related flags removed.

    Handles three flag shapes:

    - Boolean-only flags (``--continue`` / ``-c``): drop the single
      token.
    - Optional-value flags (``--resume`` / ``-r``, plus the legacy
      ``--session`` / ``-s``): if followed by a non-flag token, drop
      both; otherwise drop just the flag.
    - Long-form ``--key=value`` (``--resume=<id>`` /
      ``--session=<id>``): drop the single combined token.

    :param argv: Parent's launch argv, e.g.
        ``["python", "-m", "omnigent.cli", "run", "agent.yaml",
        "--model", "my-model", "--resume"]``.
    :returns: The same argv with resume flags removed. Other flags
        (``--model``, ``--harness``, etc.) survive untouched.
    """
    out: list[str] = []
    skip_next = False
    for idx, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token in _RESUME_BOOLEAN_FLAGS:
            continue
        if token in _RESUME_OPTIONAL_VALUE_FLAGS:
            next_token = argv[idx + 1] if idx + 1 < len(argv) else None
            if next_token is not None and not next_token.startswith("-"):
                skip_next = True
            continue
        # ``--resume=value`` / ``--session=value`` long-form.
        if "=" in token:
            head = token.split("=", 1)[0]
            if head in _RESUME_OPTIONAL_VALUE_FLAGS:
                continue
        out.append(token)
    return out

def _strip_one_shot_flags(argv: list[str]) -> list[str]:
    """
    Return *argv* with one-shot conversation flags
    (``-p``/``--prompt``/``--system-prompt``) removed.

    Same flag-shape handling as :func:`_strip_resume_flags`. The
    parent's ``-p "do X"`` was for the parent's first user turn;
    re-applying it in a sibling pane would silently auto-send the
    same prompt, surprising the user.
    """
    out: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in _ONE_SHOT_VALUED_FLAGS:
            skip_next = True
            continue
        if "=" in token:
            head = token.split("=", 1)[0]
            if head in _ONE_SHOT_VALUED_FLAGS:
                continue
        out.append(token)
    return out

