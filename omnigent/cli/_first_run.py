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

def _peek_default_agent_harness(target: str) -> str | None:
    """
    Return the canonical harness declared by a default-agent YAML, or ``None``.

    Reads ``executor.harness`` / ``executor.type`` from a local YAML path so
    :func:`_resolve_default_agent_target` can compare it to an explicit
    ``--harness``. Returns ``None`` for URLs, missing/unreadable files, or
    specs that declare no harness — the caller treats ``None`` as "cannot
    confirm a match".

    :param target: The configured ``default_agent`` value, e.g.
        ``"/Users/me/.omnigent/agents/databricks_coding_agent.yaml"``.
    :returns: The canonical harness, e.g. ``"openai-agents-sdk"``, or ``None``.
    """
    if "://" in target:
        return None
    path = Path(target).expanduser()
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    executor = raw.get("executor")
    if not isinstance(executor, dict):
        return None
    declared = executor.get("harness") or executor.get("type")
    if not isinstance(declared, str) or not declared:
        return None
    return canonicalize_harness(declared) or declared

class _FirstRunPlan:
    """The harness + optional default agent a bare ``run`` should launch.

    Derived fresh from the configured credentials on each bare ``run`` and
    never persisted (see :func:`_resolve_first_run_plan`).

    :param harness: The canonical harness id to launch, e.g. ``"claude-sdk"``.
    :param agent: The default agent target to launch (the bundled polly path
        for Claude), or ``None`` for a bare harness REPL (codex / pi).
    """

    harness: str
    agent: str | None

def _pick_first_run_harness() -> _FirstRunPlan | None:
    """Pick the harness a bare first ``run`` should launch, by configured creds.

    Priority Claude → Codex → Pi over the ambient-merged config (a detected env
    key / CLI login counts as configured). Claude gets the bundled polly
    orchestrator as its default agent; Codex / Pi launch a bare harness REPL.
    Shared with ``configure harnesses`` via
    :func:`~omnigent.onboarding.provider_config.default_provider_for_harness`,
    so the two surfaces agree on "what's configured".

    :returns: A :class:`_FirstRunPlan`, or ``None`` when no harness has a usable
        credential.
    """
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import (
        default_provider_for_harness,
        load_config,
    )

    config = effective_config_with_detected(load_config())
    if default_provider_for_harness(config, "claude-sdk") is not None:
        return _FirstRunPlan(harness="claude-sdk", agent=_bundled_example_path("polly"))
    if default_provider_for_harness(config, "codex") is not None:
        return _FirstRunPlan(harness="codex", agent=None)
    if default_provider_for_harness(config, "pi") is not None:
        return _FirstRunPlan(harness="pi", agent=None)
    return None

def _resolve_first_run_plan() -> _FirstRunPlan | None:
    """Resolve the harness + default agent for a bare ``omnigent run``.

    Adopts ambient-detected credentials, then picks a harness from what's
    configured (Claude→polly / Codex / Pi). When nothing is configured,
    prints a notice, drops the user into ``configure harnesses``, then
    re-checks once.

    The pick is **deliberately not persisted** as a global default: it is
    derived state, recomputed on every bare ``run`` from the *current*
    credentials. So a user who starts with only Codex (→ a codex REPL) and
    later adds Claude is promoted to polly on their next bare ``run`` —
    keeping polly as the primary experience — rather than being pinned to
    the earlier fallback. An *explicit* default (a user-set global
    ``harness`` / ``default_agent``, or ``run <agent>`` / ``--harness``)
    still short-circuits this path upstream and is always honored.

    :returns: The chosen :class:`_FirstRunPlan`, or ``None`` when the user still
        has no configured harness after the configure step — the caller exits
        cleanly rather than erroring.
    """
    # Adopt any ambient creds so a detected key/login becomes a real provider
    # default, exactly as opening `configure harnesses` does (and announce what
    # was auto-configured, so a never-set-up user sees which credentials we
    # picked up). This persists *credentials* (the provider layer), NOT the
    # agent/harness pick — the pick stays ephemeral so it tracks whatever creds
    # are currently available.
    _adopt_ambient_credentials()

    plan = _pick_first_run_harness()
    if plan is None:
        click.secho("Found no harnesses configured.", fg="yellow", err=True)
        _run_configure_harnesses_interactive()
        plan = _pick_first_run_harness()
    return plan

def _resolve_default_agent_target(
    default_agent: str | None,
    requested_harness: str | None,
) -> str | None:
    """
    Decide the ``run`` target when no AGENT was passed on the command line.

    - No ``default_agent`` → ``None`` (the no-AGENT ``--harness`` launcher
      builds an ad-hoc spec, or ``run`` errors when no harness either).
    - No ``--harness`` → the ``default_agent`` (the configured default
      experience, unchanged).
    - ``--harness X`` given with a ``default_agent`` whose harness is ``Y``:
      use the ``default_agent`` when ``Y == X`` (harness matches, so the user
      gets their richer configured agent); otherwise **warn** and return
      ``None`` so a minimal built-in ``X`` agent launches instead of forcing
      ``X`` onto a ``Y``-shaped spec (which would, e.g., point claude-sdk at a
      gpt model and 400 with an API-type mismatch). When ``Y`` can't be
      determined, fall back to the minimal launcher silently (can't assert a
      mismatch, but also can't confirm a match).

    :param default_agent: The configured ``default_agent`` value, or ``None``.
    :param requested_harness: The explicit ``--harness`` value, or ``None``.
    :returns: The target to run (``default_agent`` path) or ``None`` to use
        the no-AGENT launcher.
    """
    if not default_agent:
        return None
    if requested_harness is None:
        return default_agent
    requested = canonicalize_harness(requested_harness) or requested_harness
    default_harness = _peek_default_agent_harness(str(default_agent))
    if default_harness == requested:
        return default_agent
    if default_harness is not None:
        click.echo(
            f"omnigent: default agent '{default_agent}' uses harness "
            f"{default_harness!r}, but you specified --harness {requested!r}; "
            f"launching a minimal built-in {requested!r} agent instead.",
            err=True,
        )
    return None

