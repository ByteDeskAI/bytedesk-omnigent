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

def _migrate_legacy_state_dir() -> None:
    """
    One-time relocation of a pre-rename state directory to ``~/.omnigent``.

    Earlier releases stored all per-user state under ``~/.omniagents`` and then
    ``~/.omnigents`` as the name evolved. To avoid silently losing that state,
    move the newest surviving legacy directory to ``~/.omnigent`` on first run,
    but only when **all** of the following hold:

    - the new ``~/.omnigent`` does not yet exist (never clobber new state),
    - at least one directory in :data:`_LEGACY_STATE_DIRS` exists,
    - neither :data:`_CONFIG_HOME_ENV_VAR` nor :data:`_DATA_DIR_ENV_VAR` is set
      (an operator who redirects state elsewhere manages it themselves), and
    - no live host daemon is running out of that legacy directory -- moving its
      pidfile / socket dir out from under a running daemon would wedge it.

    On failure the migration is skipped with a warning rather than crashing the
    CLI; a fresh ``~/.omnigent`` is then created normally and the legacy
    directory is left untouched for the user to migrate by hand. Idempotent:
    once ``~/.omnigent`` exists this is a no-op.

    :returns: ``None``.
    """
    if _STATE_DIR.exists():
        return
    if os.environ.get(_CONFIG_HOME_ENV_VAR) or os.environ.get(_DATA_DIR_ENV_VAR):
        return
    legacy_src = next((d for d in _LEGACY_STATE_DIRS if d.exists()), None)
    if legacy_src is None:
        return

    # Guard: a daemon spawned by the old release may still be running with its
    # pidfile + unix socket under the legacy dir. Relocating those would leave
    # the daemon orphaned and the CLI unable to find it.
    legacy_pid_file = legacy_src / "host.pid"
    if legacy_pid_file.exists():
        try:
            first_line = legacy_pid_file.read_text().strip().splitlines()[0]
            legacy_pid = int(first_line)
        except (ValueError, OSError, IndexError):
            legacy_pid = None
        if legacy_pid is not None and _pid_alive(legacy_pid):
            click.echo(
                f"Note: found pre-rename state at {legacy_src} but a host daemon "
                "is still running from it; skipping migration. Run `omnigent stop` "
                "and re-run to migrate, or move it manually to ~/.omnigent.",
                err=True,
            )
            return

    try:
        shutil.move(str(legacy_src), str(_STATE_DIR))
    except OSError as exc:
        click.echo(
            f"Note: could not migrate {legacy_src} to ~/.omnigent ({exc}); "
            f"starting with fresh state. Your old data is untouched at {legacy_src}.",
            err=True,
        )
        return
    click.echo(f"Migrated per-user state from {legacy_src} to ~/.omnigent.", err=True)

def _display_path(path: Path) -> str:
    """
    Format a filesystem path for display, collapsing the home prefix to ``~``.

    A path under the user's home directory is shown as ``~/...`` for
    readability; anything else is shown as its plain string. Unlike a
    hardcoded ``~/.omnigent/...`` literal, this reflects the *actual*
    effective path — so a state dir outside ``$HOME`` (an
    ``OMNIGENT_CONFIG_HOME`` / ``OMNIGENT_DATA_DIR`` override) renders as
    its real location rather than a misleading ``~``.

    :param path: The path to display, e.g.
        ``Path("/Users/alice/.omnigent/logs/server/local-server-ab12.log")``.
    :returns: ``"~/.omnigent/..."`` when *path* is under ``$HOME``,
        otherwise ``str(path)``.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        # Not under $HOME (e.g. an OMNIGENT_DATA_DIR outside home).
        return str(path)

def _resolve_auto_open_conversation_setting(cfg: dict[str, Any]) -> bool | None:  # type: ignore[explicit-any]
    """
    Resolve the explicit ``auto_open_conversation`` config value, if set.

    Tri-state on purpose so callers can distinguish "the user has not
    expressed a preference" (``None``) from an explicit opt-in/opt-out.
    ``omnigent run`` uses this to default the browser-open ON for
    interactive launches while still honoring an explicit
    ``auto_open_conversation: false``; see :func:`run`.

    :param cfg: Effective config dict from :func:`_load_effective_config`,
        e.g. ``{"auto_open_conversation": True}``.
    :returns: ``True`` / ``False`` when the key is present, or ``None``
        when the user has not configured it.
    :raises click.ClickException: If the configured value is not a
        supported boolean.
    """
    raw = cfg.get(_AUTO_OPEN_CONVERSATION_CONFIG_KEY)
    if raw is None:
        return None
    return _parse_config_bool(_AUTO_OPEN_CONVERSATION_CONFIG_KEY, raw)

def _default_artifact_location() -> str:
    """Default artifact dir for ``omnigent server`` — ``<data_dir>/artifacts``.

    Kept in lock-step with :func:`_default_db_uri` so a default-config
    ``omnigent server`` and ``omnigent run`` share one coherent
    machine-global instance (same DB *and* same artifacts) — otherwise a
    conversation created by one would reference files the other can't
    resolve. ``--artifact-location`` / the config file still override.

    :returns: e.g. ``"/home/alice/.omnigent/artifacts"``.
    """
    from omnigent.host.local_server import _local_data_dir

    return str(_local_data_dir() / "artifacts")

def _is_relative_artifact_location(location: str) -> bool:
    """Return whether a config artifact location should resolve against the config file."""
    return (
        "://" not in location
        and not location.startswith("dbfs:/")
        and not Path(location).is_absolute()
    )

def _ensure_sqlite_parent_dir(db_uri: str) -> None:
    """Create the parent directory of a SQLite DB file if it's missing.

    SQLite creates the ``.db`` file on first connect but **not** its
    parent directory — an absent parent raises ``sqlite3.OperationalError:
    unable to open database file``. The default ``server`` DB now lives at
    ``<data_dir>/chat.db`` (machine-global, honoring ``OMNIGENT_DATA_DIR``),
    so a first-ever run — or any run after the data dir was cleared — must
    create that dir before the stores connect. The daemon-spawned server
    handles this in ``ensure_local_omnigent_server``; this is the equivalent for
    the foreground ``omnigent server`` command.

    No-op for non-SQLite URIs (Postgres etc.) and for in-memory SQLite.

    :param db_uri: The resolved store DB URI, e.g.
        ``"sqlite:////home/alice/.omnigent/chat.db"`` or
        ``"postgresql://host/db"``.
    :returns: None.
    """
    from sqlalchemy.engine import make_url

    url = make_url(db_uri)
    if url.get_backend_name() != "sqlite":
        return
    # url.database is the filesystem path for file-backed SQLite, None or
    # ":memory:" for in-memory — neither needs a parent dir.
    if not url.database or url.database == ":memory:":
        return
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)

def main() -> None:
    """
    Console-script entry point for ``omnigent``.

    Dispatches to the click CLI for subcommands like ``run``,
    ``attach``, and ``server``. The removed top-level ad-hoc chat
    shape (``omnigent [--flags] [prompt]``) is rejected here so it
    cannot fall back to the legacy in-process runner path.

    Also inserts the current working directory at ``sys.path[0]``
    so dotted callables declared in user YAMLs (``callable:
    mypackage.mymodule.my_fn``) resolve against the user's project,
    not the console-script's install directory. Console entry
    points put the script's own directory at sys.path[0] by
    default, which is almost never what a CLI that imports
    user-authored modules wants.

    Sets up the always-on CLI diagnostics log before Click dispatch
    so unhandled exceptions are captured even when the user didn't
    enable ``--log`` or ``--debug-events``.
    """
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    # Relocate pre-rename ~/.omniagents state before anything reads ~/.omnigent
    # (update-check cache, diagnostics logs, config). No-op once migrated.
    _migrate_legacy_state_dir()

    argv = sys.argv[1:]

    # Bare ``omnigent`` with no args behaves like ``omnigent run`` on an
    # interactive terminal: ``run`` resolves the configured default agent /
    # first-run plan and drops into ``setup`` when nothing is configured. In
    # a non-interactive context (pipe, CI, no TTY) fall back to ``--help`` so
    # we never launch a REPL that would hang waiting on stdin.
    if not argv:
        argv = ["run"] if sys.stdin.isatty() else ["--help"]

    # Shorthand: ``omnigent --harness claude [opts]`` →
    # ``run --harness claude [opts]``. Click group-level options are
    # intentionally tiny (currently only help/version); runner flags live on
    # ``run``. Treat a leading non-top-level flag as bare-run shorthand so
    # users can type the natural no-AGENT launcher form.
    if argv and argv[0].startswith("-") and argv[0] not in {"--help", "-h", "--version"}:
        argv = ["run", *argv]

    # Shorthand: ``omnigent myagent.yaml [opts]`` → ``run myagent.yaml [opts]``.
    # Allows ``omnigent`` to act as a transparent alias for ``omnigent run``
    # when the first positional argument is an agent path.
    if _is_run_shorthand(argv):
        argv = ["run", *argv]

    if argv and _is_server_url(argv[0]):
        click.echo(
            "Error: server URLs must be passed with --server. "
            f"Use `omnigent run --server {argv[0]}`.",
            err=True,
        )
        raise SystemExit(2)

    if _is_removed_ad_hoc_invocation(argv):
        click.echo(
            "Error: top-level ad-hoc chat was removed. Use "
            "`omnigent run <agent.yaml>` or "
            "`omnigent run --harness <harness>`.",
            err=True,
        )
        raise SystemExit(2)

    # Always-on diagnostics — captures exceptions, lifecycle events,
    # and warnings to ~/.omnigent/logs/cli-*.log even when --log
    # (conversation JSON) and --debug-events (SSE tape) are off.
    # Skip for pure help/version so quick invocations don't create
    # log litter.
    if argv[0] in {"--help", "-h", "--version"}:
        cli(args=argv)
        return

    from omnigent.cli_diagnostics import (
        log_cli_error_hint,
        log_cli_exception,
        print_setup_hint,
        setup_cli_logging,
    )

    setup_cli_logging(argv)

    # ``omnigent setup`` IS the setup wizard — if it fails, telling the
    # user to "run omnigent setup" would be circular. ``upgrade`` is
    # excluded too: its failures (unreachable index, dev checkout, install
    # error) are never about a missing model credential, so the setup hint
    # would only mislead.
    suggest_setup = argv[0] not in {"setup", "upgrade"}

    # Lightweight update notice: only on an interactive terminal and only
    # for user-facing commands. Reads a cached "latest PyPI version" and
    # prints at most once per release (the network refresh runs detached,
    # off the hot path). Never blocks; any failure is swallowed inside.
    if not _should_skip_update_check(argv) and sys.stderr.isatty():
        from omnigent.update_check import maybe_show_update_notice

        maybe_show_update_notice()

    try:
        cli(args=argv, standalone_mode=False)
    except click.ClickException as exc:
        log_cli_exception(exc, prefix="Click CLI error")
        exc.show()
        if suggest_setup:
            print_setup_hint()
        raise SystemExit(exc.exit_code) from exc
    except click.Abort as exc:
        # Ctrl+C / user cancel — no hint, the user knows what they did.
        log_cli_exception(exc, prefix="Aborted CLI")
        click.echo("Aborted!", err=True)
        raise SystemExit(1) from exc
    except Exception as exc:
        log_cli_error_hint(exc)
        if suggest_setup:
            print_setup_hint()
        raise

def _is_run_shorthand(argv: list[str]) -> bool:
    """Return True when *argv* looks like ``omnigent <target> [opts]``
    where *target* is an agent YAML/directory rather than a subcommand.

    Used by :func:`main` to transparently redirect
    ``omnigent myagent.yaml --model m`` to
    ``omnigent run myagent.yaml --model m``.

    :param argv: CLI arguments without the program name, e.g.
        ``["myagent.yaml", "--model", "m"]``.
    :returns: ``True`` when the first positional argument looks like a
        run target (file path).
    """
    if not argv:
        return False
    first = argv[0]
    if first.startswith("-"):
        return False  # leading flag, not a positional target
    if first in _CLICK_SUBCOMMANDS:
        return False  # already a known subcommand
    if _is_server_url(first):
        return False
    # Accept paths ending with .yaml/.yml and explicit relative/absolute
    # paths. Server addresses are only accepted through ``--server``.
    return (
        first.endswith((".yaml", ".yml")) or first.startswith(("./", "../")) or (os.sep in first)
    )

def _is_removed_ad_hoc_invocation(argv: list[str]) -> bool:
    """
    Decide whether *argv* targets the removed top-level ad-hoc chat.

    True when:
    - The first non-flag token isn't a known click subcommand and is
      a quoted multi-word prompt (e.g.
      ``omnigent "what does this repo do?"``) — the free-text shape
      the removed top-level ad-hoc chat accepted.

    False when the first non-flag token matches a known
    subcommand (``omnigent run ...``, ``omnigent attach ...``),
    when the user asks for top-level help/version
    (``omnigent --help``, ``omnigent --version``), or when the
    token is a single command-shaped word (e.g. ``omnigent blah``)
    — those stay on the click path so an unknown command produces
    click's standard "No such command" error rather than the ad-hoc
    removal notice.

    :param argv: Argv without the program name, e.g.
        ``sys.argv[1:]``.
    :returns: True for removed ad-hoc dispatch, False for click dispatch.
    """
    if not argv:
        return False
    # Top-level click flags (``--help`` / ``-h`` / ``--version``)
    # should go through click so the user sees the click group's
    # help listing subcommands, not the legacy argparse help.
    if argv[0] in {"--help", "-h", "--version"}:
        return False
    # Skip leading flags to find the first positional. If all
    # tokens are flags (e.g. ``omnigent --system-prompt "..."``),
    # treat it as removed ad-hoc chat rather than handing it to click
    # as a top-level option.
    for token in argv:
        if token.startswith("-"):
            continue
        if token in _CLICK_SUBCOMMANDS:
            return False
        # A single command-shaped word (no whitespace) is an unknown
        # subcommand: hand it to click for its standard "No such
        # command" error. Only a quoted multi-word prompt matches the
        # removed top-level ad-hoc chat shape.
        return any(ch.isspace() for ch in token)
    return True

class _SessionsPageResult:
    """
    Decoded sessions page.

    :param sessions: Session rows returned by the page.
    :param last_id: Last session id in the page, e.g. ``"conv_abc123"``.
    :param has_more: Whether another page should be fetched.
    :param error: Human-readable error text, or ``None`` on success.
    """

    sessions: list[_HostSessionRow]
    last_id: str | None
    has_more: bool
    error: str | None

class _SessionPagesResult:
    """
    Accumulated sessions from a paginated query.

    :param sessions: Session rows across all fetched pages.
    :param error: Human-readable error text, or ``None`` on success.
    """

    sessions: list[_HostSessionRow]
    error: str | None

