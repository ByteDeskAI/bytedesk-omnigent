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
        "Remote omnigent URL. Starts a local runner, binds the session, "
        "launches Claude in a terminal resource, and attaches this TTY. "
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
        "opens an interactive picker scoped to claude-native sessions."
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
@click.option(
    "--host",
    "register_host",
    is_flag=True,
    default=False,
    help=(
        "Register this machine as a host (inline equivalent of `omnigent host`). "
        "Requires --server."
    ),
)
@click.option(
    "--use-native-config",
    "use_claude_config",
    is_flag=True,
    default=False,
    help=(
        "Use your existing Claude Code configuration instead of Databricks auth. "
        "When set, any configured provider is ignored and Claude "
        "authenticates via its own ``~/.claude/`` settings."
    ),
)
@click.option(
    "--profile-startup",
    "profile_startup",
    is_flag=True,
    default=False,
    help=(
        "Print native Claude startup timing marks to stderr. Also enabled by "
        f"{_CLAUDE_STARTUP_PROFILE_ENV_VAR}=1."
    ),
)
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def claude(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    register_host: bool,
    use_claude_config: bool,
    profile_startup: bool,
    claude_args: tuple[str, ...],
) -> None:
    # Param docs live in comments — Click uses the docstring for --help.
    # :param server: Remote Omnigent server URL, or None for local.
    # :param resume: None, picker sentinel, or a conversation id.
    # :param session_id: Legacy ``--session`` id; mutually exclusive with ``--resume``.
    # :param use_claude_config: When True, skip ucode/Databricks auth and use
    #     existing Claude config.
    # :param profile_startup: When True, print startup timing marks.
    # :param claude_args: Pass-through args for ``claude``.
    """Launch Claude Code in an Omnigent terminal.

    \b
    Examples:
      omnigent claude
      omnigent claude --resume conv_abc123
      omnigent claude --resume                  # interactive picker
      omnigent claude --server https://<app>.databricksapps.com
    """
    startup_profiler = StartupProfiler.from_env(
        name="omnigent claude",
        env_var=_CLAUDE_STARTUP_PROFILE_ENV_VAR,
        explicit=profile_startup,
    )
    startup_profiler.mark("cli entered")

    # Apply config defaults (same as ``run`` does).
    cfg = __facade_binding("_load_effective_config", _load_effective_config)()
    if server is None:
        server = cfg.get("server")
    auto_open_conversation = __facade_binding(
        "_resolve_auto_open_conversation_from_config",
        _resolve_auto_open_conversation_from_config,
    )(cfg)
    startup_profiler.mark("config resolved")

    # Validate option combinations BEFORE any side effects (daemon
    # spawn, server discovery). Calling _ensure_backend first would
    # mean a bad arg pair waits the full local-server-discover
    # timeout (60s in CI) before surfacing the UsageError, which
    # the test_claude_command_session_and_resume_mutually_exclusive
    # regression caught in CI.
    del register_host
    choice = __facade_binding("_split_resume_value", _resume_split_resume_value)(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session and --resume are mutually exclusive; "
            "prefer --resume (--session is deprecated).",
        )
    startup_profiler.mark("arguments validated")

    # Ensure the host daemon (local when ``--server`` is omitted/empty,
    # remote otherwise) and resolve the concrete Omnigent server URL. The daemon
    # owns the runner; the CLI only connects. ``--host`` is now redundant
    # (the daemon is always ensured) and kept only as a no-op for scripts.
    startup_profiler.mark("ensuring backend")
    server = __facade_binding("_ensure_backend", _ensure_backend)(server)
    startup_profiler.mark("backend ready", detail=f"server={server}")

    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    from omnigent.claude_native import run_claude_native

    startup_profiler.mark("native module imported")

    run_claude_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        claude_args=claude_args,
        use_claude_config=use_claude_config,
        auto_open_conversation=auto_open_conversation,
        startup_profiler=startup_profiler,
    )
