"""CLI entry point for omnigent."""

from __future__ import annotations

import os
import sys

import click

from ._helpers import _migrate_legacy_state_dir
from ._version import _print_version_callback, _should_skip_update_check


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state

    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()


@click.group()
@click.option(
    "--version",
    is_flag=True,
    callback=_print_version_callback,
    expose_value=False,
    is_eager=True,
    help="Show the version and exit.",
)
def cli() -> None:
    """Omnigent CLI."""


# Names of every subcommand the click group owns. Used by
# :func:`main` to reject the removed top-level ad-hoc chat path
# before click reports an opaque "no such command" error.
# Keep in sync with ``@cli.command()`` decorations below.
_CLICK_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "attach",
        "claude",
        "codex",
        "config",
        "debby",
        "debug",
        "host",
        "lakebox",
        "login",
        "pane-picker",
        "pane-split",
        "pi",
        "polly",
        "resume",
        "run",
        "sandbox",
        "server",
        "setup",
        "stop",
        "upgrade",
        "version",
    }
)


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


def _is_server_url(value: str) -> bool:
    """Return whether *value* is a server URL.

    :param value: CLI argument value, e.g. ``"http://localhost:6767"``.
    :returns: ``True`` for ``http://`` or ``https://`` URLs.
    """
    return value.startswith(("http://", "https://"))


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