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

@cli.command("pane-split", hidden=True)
@click.option("-v", "direction", flag_value="v", help="Vertical split (new pane below)")
@click.option(
    "-h",
    "direction",
    flag_value="h",
    help="Horizontal split (new pane to the right)",
)
@click.option("-w", "direction", flag_value="w", help="New window/tab")
@click.option(
    "-p",
    "--parent-pane",
    "parent_pane",
    required=True,
    help="Tmux pane id of the parent omnigent pane (e.g. '%0'). "
    "Forwarded by the wrapped key-binding via #{pane_id}.",
)
def pane_split(direction: str | None, parent_pane: str) -> None:
    """
    Split the parent omnigent pane and run the chooser in the new pane.

    Internal subcommand invoked by the tmux key-binding wrappers
    installed by ``omnigent.repl._tmux_pane``. The wrapper fires
    ``run-shell 'omnigent pane-split -<v|h|w> -p #{pane_id}'`` when
    the user presses their split key while focused on an omnigent
    pane; tmux substitutes ``#{pane_id}`` to the focused pane's id
    and we exec the right ``tmux split-window`` / ``new-window``
    invocation pointing at ``omnigent pane-picker``.

    :param direction: One of ``v`` / ``h`` / ``w``. Required.
    :param parent_pane: The omnigent pane id, e.g. ``%0``. Required.
    """
    import shlex

    from omnigent.repl._tmux_pane import _resolve_omnigent_argv

    if direction not in _PANE_SPLIT_DIRECTIONS:
        raise click.ClickException("pane-split requires exactly one of -v, -h, or -w")
    # The new pane runs ``omnigent pane-picker`` which reads the
    # parent's pane options and exec's into the chosen agent run.
    # We pass the parent pane id explicitly because the new pane's
    # ``$TMUX_PANE`` will be the new pane, not the parent.
    #
    # tmux's ``split-window`` / ``new-window`` spawns the new
    # pane's initial command via ``/bin/sh -c``, and that shell
    # inherits the tmux server's PATH — which typically does NOT
    # include the venv ``bin/`` where ``omnigent`` lives.
    # ``_resolve_omnigent_argv`` returns either an absolute
    # path to the binary (preferred) or ``[python, "-m",
    # "omnigent.cli"]`` as a fallback that always works.
    picker_argv = [
        *_resolve_omnigent_argv(),
        "pane-picker",
        "--parent-pane",
        parent_pane,
    ]
    picker_cmd = " ".join(shlex.quote(p) for p in picker_argv)
    # Resolve the parent pane's working directory and pass it via
    # ``-c`` so the new pane inherits the same cwd. Without this,
    # tmux's ``split-window`` / ``new-window`` defaults to the
    # tmux server's cwd (often the user's HOME), which means
    # relative agent paths in the parent's launch argv (e.g.
    # ``examples/databricks_coding_agent.yaml``) don't resolve in
    # the new pane and the spawned REPL exits with "agent path
    # not found" within seconds.
    parent_cwd = subprocess.run(
        ["tmux", "display-message", "-p", "-t", parent_pane, "-F", "#{pane_current_path}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    cwd_args = ["-c", parent_cwd] if parent_cwd else []
    if direction == "v":
        argv = ["tmux", "split-window", "-v", "-t", parent_pane, *cwd_args, picker_cmd]
    elif direction == "h":
        argv = ["tmux", "split-window", "-h", "-t", parent_pane, *cwd_args, picker_cmd]
    else:  # "w"
        argv = ["tmux", "new-window", *cwd_args, picker_cmd]
    os.execvp("tmux", argv)


