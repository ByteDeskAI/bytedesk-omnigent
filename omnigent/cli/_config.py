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


def __facade_binding(name: str, fallback):
    import omnigent.cli as cli_facade

    return getattr(cli_facade, name, fallback)


def _load_config(path: str | None) -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Load and return config from a YAML file.
    Returns an empty dict if no path is provided.
    """
    if path is None:
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}

def _server_uvicorn_log_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Return Uvicorn logging config with request-duration access logs.

    Uvicorn emits the FastAPI access line itself, so Omnigent swaps
    only the access formatter while preserving Uvicorn's default
    handlers, levels, and server-log formatting.

    :returns: Uvicorn ``log_config`` suitable for ``uvicorn.run``.
    """
    import uvicorn.config

    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["formatters"]["access"]["()"] = (
        "omnigent.server.performance_metrics.RequestDurationAccessFormatter"
    )
    return log_config

def _effective_global_config_path() -> Path:
    """
    Return the path to the user-level Omnigent config.

    :returns: ``$OMNIGENT_CONFIG_HOME/config.yaml`` when the env
        override is set, otherwise :data:`_GLOBAL_CONFIG_PATH`.
    """
    if config_home := os.environ.get(_CONFIG_HOME_ENV_VAR):
        return Path(config_home) / "config.yaml"
    return _GLOBAL_CONFIG_PATH

def _display_config_path(path: Path) -> str:
    """
    Format a config path for display, collapsing the home prefix to ``~``.

    Thin wrapper over :func:`_display_path` kept for call-site readability
    where the path is specifically the effective config file.

    :param path: The config path to display, e.g.
        ``Path("/Users/alice/.omnigent/config.yaml")``.
    :returns: ``"~/.omnigent/config.yaml"`` when *path* is under
        ``$HOME``, otherwise ``str(path)``.
    """
    return _display_path(path)

def _load_global_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Load the global omnigent config from ``~/.omnigent/config.yaml``.

    Returns an empty dict when the file does not exist or is empty.
    Top-level default keys (``default_agent``, ``server``,
    ``model``, ``harness``) hold plain string values.  The optional
    ``auto_open_conversation`` key is a boolean. The optional
    ``auth:`` key holds a nested mapping —
    ``{"type": "databricks", "profile": "oss"}`` or
    ``{"type": "api_key", "api_key": "…"}`` — written by
    ``omnigent setup`` and used by the runtime to supply executor
    credentials when an agent spec does not declare ``executor.auth``.

    :returns: Parsed YAML as a dict, e.g.
        ``{"default_agent": "examples/hello_world.yaml",
        "auth": {"type": "databricks", "profile": "oss"}}``.
    """
    path = _effective_global_config_path()
    if not path.exists():
        return {}
    with open(path) as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}  # type: ignore[explicit-any]
        return raw

def _load_local_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Load the project-level config from ``.omnigent/config.yaml`` in cwd.

    Returns an empty dict when the file does not exist or is empty.

    :returns: Parsed YAML as a dict.
    """
    path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    if not path.exists():
        return {}
    with open(path) as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}  # type: ignore[explicit-any]
        return raw

def _load_effective_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Merge global and project-level config.

    Precedence (highest last): global (``~/.omnigent/config.yaml``)
    → local (``.omnigent/config.yaml`` in cwd).  Project config
    always wins so per-repo settings override user defaults.

    :returns: Merged config dict.
    """
    load_global_config = __facade_binding("_load_global_config", _load_global_config)
    load_local_config = __facade_binding("_load_local_config", _load_local_config)
    return {**load_global_config(), **load_local_config()}

def _parse_config_bool(key: str, value: _ConfigValue) -> bool:
    """
    Parse a boolean value from YAML or ``omnigent config KEY=VALUE``.

    :param key: Config key being parsed, e.g.
        ``"auto_open_conversation"``.
    :param value: Raw value from YAML or CLI parsing, e.g. ``"true"``.
    :returns: Parsed boolean value.
    :raises click.ClickException: If *value* is not a supported boolean.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _CONFIG_TRUE_VALUES:
            return True
        if normalized in _CONFIG_FALSE_VALUES:
            return False
    raise click.ClickException(
        f"Config key {key!r} must be a boolean (true/false, yes/no, on/off, or 1/0)."
    )

def _resolve_auto_open_conversation_setting(cfg: dict[str, Any]) -> bool | None:  # type: ignore[explicit-any]
    """
    Resolve the explicit ``auto_open_conversation`` config value, if set.

    :param cfg: Effective config dict from :func:`_load_effective_config`.
    :returns: ``True`` / ``False`` when present, otherwise ``None``.
    :raises click.ClickException: If the configured value is not a
        supported boolean.
    """
    raw = cfg.get(_AUTO_OPEN_CONVERSATION_CONFIG_KEY)
    if raw is None:
        return None
    return _parse_config_bool(_AUTO_OPEN_CONVERSATION_CONFIG_KEY, raw)

def _resolve_auto_open_conversation_from_config(cfg: dict[str, Any]) -> bool:  # type: ignore[explicit-any]
    """
    Resolve whether CLI launches should open conversation URLs.

    Defaults to ``False`` when the user has not configured the key.
    ``omnigent run`` does not use this resolver — it defaults the
    browser-open ON for interactive launches via
    :func:`_resolve_auto_open_conversation_setting`.

    :param cfg: Effective config dict from :func:`_load_effective_config`,
        e.g. ``{"auto_open_conversation": True}``.
    :returns: ``True`` when conversation links should be opened
        automatically.
    :raises click.ClickException: If the configured value is not a
        supported boolean.
    """
    setting = _resolve_auto_open_conversation_setting(cfg)
    return setting if setting is not None else False

def _save_global_config(  # type: ignore[explicit-any]
    # Any (matching the yaml-boundary helpers above): config values are
    # heterogeneous YAML scalars and nested mappings — e.g. the providers:
    # block, whose entries come back as dict[str, object] from
    # provider_entry_settings / set_default_provider. _ConfigValue can't
    # express that interop without invariance errors against those object
    # returns, so this stays the same Any boundary _load_*_config uses.
    settings: Mapping[str, Any],
    unset_keys: tuple[str, ...] = (),
    deep_merge_keys: tuple[str, ...] = (),
) -> None:
    """
    Merge *settings* into ``~/.omnigent/config.yaml`` and remove any
    keys listed in *unset_keys*.

    Creates the ``~/.omnigent/`` directory if it does not exist.
    Values may be plain strings, booleans, or nested mappings (the
    ``auth:`` block written by ``omnigent setup``, or a ``providers:``
    block written by ``omnigent setup --no-internal-beta``).

    By default every key in *settings* **replaces** the existing value
    wholesale (a shallow ``dict.update``). For keys listed in
    *deep_merge_keys*, the incoming mapping is instead merged one level
    deep into the existing mapping for that key — so passing a single
    provider under ``providers:`` adds/updates that one entry without
    dropping the others. Use the default (shallow replace) when the new
    mapping must become the *entire* block (e.g. after
    :func:`~omnigent.onboarding.provider_config.set_default_provider`,
    which clears sibling ``default`` flags a deep-merge could not reach).

    :param settings: Key/value pairs to set, e.g.
        ``{"default_agent": "/abs/path/agent.yaml",
        "auto_open_conversation": True,
        "auth": {"type": "databricks", "profile": "oss"}}``.
    :param unset_keys: Keys to remove from the config, e.g.
        ``("server",)``.
    :param deep_merge_keys: Keys whose mapping value should be merged
        one level deep into the existing mapping rather than replacing
        it, e.g. ``("providers",)`` to add one provider entry without
        dropping the rest.
    """
    cfg = _load_global_config()
    for key, value in settings.items():
        if key in deep_merge_keys and isinstance(value, Mapping):
            existing = cfg.get(key)
            merged = dict(existing) if isinstance(existing, Mapping) else {}
            merged.update(value)
            cfg[key] = merged
        else:
            cfg[key] = value
    for key in unset_keys:
        cfg.pop(key, None)
    path = _effective_global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=True)

def _save_local_config(
    settings: dict[str, str | bool],
    unset_keys: tuple[str, ...] = (),
) -> None:
    """
    Merge *settings* into ``.omnigent/config.yaml`` in cwd and remove
    any keys listed in *unset_keys*.

    Creates the ``.omnigent/`` directory if it does not exist.

    :param settings: Key/value pairs to set, e.g.
        ``{"default_agent": "examples/agent.yaml",
        "auto_open_conversation": True}``.
    :param unset_keys: Keys to remove from the config, e.g.
        ``("server",)``.
    """
    path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    cfg = _load_local_config()
    cfg.update(settings)
    for key in unset_keys:
        cfg.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=True)

class _ConfigGroup(click.Group):
    """``config`` group that nudges the pre-split flat form to the subcommands.

    Before the noun-verb split, ``config`` took a positional ``KEY=VALUE``
    plus ``--list`` / ``--unset`` / ``--global`` flags. Those now live under
    ``config set`` / ``config list`` / ``config unset``. Click's default
    error for the old form is opaque (``No such command 'x=y'`` / ``No such
    option: --list``), so this intercepts the legacy first token and raises
    a hint pointing at the new command instead.
    """

    @staticmethod
    def _legacy_hint(first: str) -> str | None:
        """Return a migration hint for a legacy first token, else ``None``.

        :param first: The first CLI token after ``config``, e.g.
            ``"--list"`` or ``"model=gpt-5.4-mini"``.
        :returns: A hint string for a recognized legacy form, else ``None``.
        """
        if first == "--list":
            return "`config --list` is now `omnigent config list`."
        if first == "--unset":
            return "`config --unset KEY` is now `omnigent config unset KEY`."
        if first == "--global":
            return (
                "`--global` now goes on the subcommand — "
                "`omnigent config set --global KEY=VALUE` or "
                "`omnigent config unset --global KEY`."
            )
        if "=" in first and not first.startswith("-"):
            return f"setting defaults is now `omnigent config set {first}`."
        return None

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Intercept the legacy flat form before normal group parsing.

        :param ctx: The click context.
        :param args: Raw argument tokens after ``config``.
        :returns: The remaining args from the base parser (for valid forms).
        :raises click.UsageError: When the first token is a legacy form, with
            a hint pointing at the new ``config set`` / ``list`` / ``unset``.
        """
        # Only the FIRST token is inspected: a known subcommand (set/list/
        # unset) parses normally — so ``config set default_agent=x`` is not
        # mistaken for the legacy ``config default_agent=x``.
        if args and args[0] not in self.commands:
            hint = self._legacy_hint(args[0])
            if hint is not None:
                raise click.UsageError(hint)
        return super().parse_args(ctx, args)


