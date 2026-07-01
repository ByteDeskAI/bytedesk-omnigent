from __future__ import annotations

import click

from .._core import cli

def _import_package_bindings() -> None:
    from .. import _constants as _pkg_constants
    from .. import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _import_helper_bindings() -> None:
    from .. import _config as _m__config
    from .. import _daemon as _m__daemon
    from .. import _deploy as _m__deploy
    from .. import _first_run as _m__first_run
    from .. import _helpers as _m__helpers
    from .. import _host_ui as _m__host_ui
    from .. import _pane as _m__pane
    from .. import _runner_proc as _m__runner_proc
    from .. import _server as _m__server
    from .. import _version as _m__version
    g = globals()
    for _key, _value in _m__config.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__daemon.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__deploy.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__first_run.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__helpers.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__host_ui.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__pane.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__runner_proc.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__server.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value
    for _key, _value in _m__version.__dict__.items():
        if not _key.startswith("__"):
            g[_key] = _value


_import_helper_bindings()

@cli.command("pane-picker", hidden=True)
@click.option(
    "--parent-pane",
    "parent_pane",
    required=True,
    help="Tmux pane id of the parent omnigent pane (e.g. '%0'). "
    "Used to read launch context (agent name, launch argv, server URL) "
    "from custom pane options the parent set via "
    "``omnigent.repl._tmux_pane.register_pane``.",
)
def pane_picker(parent_pane: str) -> None:
    """
    Launch a fresh REPL conversation in the current new pane.

    Internal subcommand. The new tmux pane (created by
    ``omnigent pane-split``) execs this command, which:

    1. Reads the parent omnigent pane's ``@omnigent-launch-argv``
       and friends.
    2. ``os.execvp``\\s the parent's launch argv to spawn a new
       REPL against the same agent in this pane.

    v1 has exactly one path: "new conversation with the same
    agent". A chooser dialog (sub-agent listing, "continue
    sub-agent X", etc.) lands in Phase 2 — see
    ``designs/REPL_TMUX_PANE_SPLIT.md``. With only one option,
    a chooser is friction; we just exec.

    :param parent_pane: The parent omnigent pane id, e.g. ``%0``.
    """
    import json

    from omnigent.repl._tmux_pane import (
        OPT_LAUNCH_ARGV,
        read_pane_option,
    )

    launch_argv_json = read_pane_option(parent_pane, OPT_LAUNCH_ARGV)
    if not launch_argv_json:
        click.echo(
            f"error: parent pane {parent_pane} has no omnigent context "
            f"(missing {OPT_LAUNCH_ARGV} option). Cannot launch sibling REPL.",
            err=True,
        )
        sys.exit(1)
    try:
        launch_argv = json.loads(launch_argv_json)
    except json.JSONDecodeError as exc:
        click.echo(
            f"error: parent pane {parent_pane}'s {OPT_LAUNCH_ARGV} option "
            f"is not valid JSON: {exc}",
            err=True,
        )
        sys.exit(1)
    if not isinstance(launch_argv, list) or not launch_argv:
        click.echo(
            f"error: parent pane {parent_pane}'s launch argv is empty or "
            f"not a list — cannot reconstruct a launch command.",
            err=True,
        )
        sys.exit(1)

    # Strip resume-related flags from the parent's argv so the new
    # pane starts a FRESH conversation instead of trying to resume
    # the parent's. The parent may have been launched with
    # ``--resume`` (bare picker), ``--resume <id>`` (specific
    # conversation pin), or ``--continue`` (latest-conv shortcut);
    # replaying them in the new pane would re-open the parent's
    # conversation, defeating the point of a sibling pane. Legacy
    # ``--session <id>`` is also handled here so pre-consolidation
    # parent argvs still sanitize cleanly.
    fresh_argv = _strip_resume_flags(launch_argv)
    # Same treatment for ``-p`` / ``--prompt`` and ``--system-prompt``:
    # the parent's auto-prompt was for THAT conversation; we don't
    # want the new pane to silently re-send it.
    fresh_argv = _strip_one_shot_flags(fresh_argv)
    os.execvp(fresh_argv[0], fresh_argv)

