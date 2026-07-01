from __future__ import annotations

import click

from .._core import cli
from .resume import _split_resume_value as _resume_split_resume_value


def __facade_binding(name: str, fallback):
    import omnigent.cli as cli_facade

    return getattr(cli_facade, name, fallback)

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

@cli.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "Remote omnigent URL. Ensures the host daemon, asks the "
        "daemon-spawned runner to launch Pi, and attaches this TTY. "
        'Pass --server "" to auto-spawn a persistent local server in the '
        "background and use that instead of a remote one."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Resume a prior Omnigent conversation. With a conversation id "
        "(e.g. ``--resume conv_abc123``) attaches directly; with no value "
        "opens an interactive picker scoped to pi-native sessions."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Deprecated alias for ``--resume <id>``; kept for one release.",
)
@click.argument("pi_args", nargs=-1, type=click.UNPROCESSED)
def pi(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    pi_args: tuple[str, ...],
) -> None:
    """Launch Pi TUI in an Omnigent terminal.

    \b
    Examples:
      omnigent pi
      omnigent pi --resume conv_abc123
      omnigent pi --resume                    # interactive picker
      omnigent pi --model local-deepseek/deepseek-v4-flash
    """
    choice = __facade_binding("_split_resume_value", _resume_split_resume_value)(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session and --resume are mutually exclusive; "
            "prefer --resume (--session is deprecated).",
        )

    from omnigent.pi_native import run_pi_native

    cfg = __facade_binding("_load_effective_config", _load_effective_config)()
    if server is None:
        server = cfg.get("server")
    auto_open_conversation = __facade_binding(
        "_resolve_auto_open_conversation_from_config",
        _resolve_auto_open_conversation_from_config,
    )(cfg)

    server = __facade_binding("_ensure_backend", _ensure_backend)(server)
    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_pi_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        pi_args=pi_args,
        auto_open_conversation=auto_open_conversation,
    )


def _run_bundled_agent(name: str, run_args: tuple[str, ...]) -> None:
    """Forward a bundled-agent subcommand to ``run`` on its packaged path.

    Implements ``omnigent polly`` / ``omnigent debby``: resolves the bundled
    example directory and re-dispatches through the ``run`` command's own
    parser, so every ``run`` flag (``--server``, ``-p``, ``--resume``, ...)
    works unchanged on the agent shorthands without duplicating ``run``'s
    option declarations.

    ``prog_name`` is pinned to ``"omnigent run"`` so context-derived output —
    usage errors and the :func:`_build_resume_parts` replay prefix — renders
    as the canonical ``omnigent run <path>`` form, which stays valid when
    replayed.

    :param name: Bundled example directory name, e.g. ``"polly"``.
    :param run_args: Unparsed pass-through CLI args for ``run``,
        e.g. ``("-p", "review the last commit")``.
    """
    # standalone_mode=False propagates ClickExceptions to main()'s handler
    # (CLI diagnostics logging + setup hint) instead of exiting inline,
    # matching the outer `cli(args=argv, standalone_mode=False)` dispatch.
    run.main(
        args=[_bundled_example_path(name), *run_args],
        prog_name="omnigent run",
        standalone_mode=False,
    )
