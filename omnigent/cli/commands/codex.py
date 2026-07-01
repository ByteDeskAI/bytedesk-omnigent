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
        "daemon-spawned runner to launch Codex, and attaches this TTY. "
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
        "opens an interactive picker scoped to codex-native sessions."
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
@click.option("--model", default=None, help="Codex model to use for the native thread.")
@click.option(
    "-p",
    "--prompt",
    default=None,
    help="Send this as the first message after the Codex TUI starts.",
)
@click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
def codex(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    model: str | None,
    prompt: str | None,
    codex_args: tuple[str, ...],
) -> None:
    # Param docs live in comments — Click uses the docstring for --help.
    # :param server: Remote Omnigent server URL, or None for local.
    # :param resume: None, picker sentinel, or a conversation id.
    # :param session_id: Legacy ``--session`` id; mutually exclusive with ``--resume``.
    # :param model: Codex model id.
    # :param prompt: Optional first prompt.
    # :param codex_args: Pass-through args for ``codex`` before ``resume``.
    """Launch Codex TUI in an Omnigent terminal.

    \b
    Examples:
      omnigent codex
      omnigent codex --resume conv_abc123
      omnigent codex --resume                  # interactive picker
      omnigent codex --server https://<app>.databricksapps.com
    """
    choice = __facade_binding("_split_resume_value", _resume_split_resume_value)(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session and --resume are mutually exclusive; "
            "prefer --resume (--session is deprecated).",
        )

    from omnigent.codex_native import run_codex_native

    cfg = __facade_binding("_load_effective_config", _load_effective_config)()
    if server is None:
        server = cfg.get("server")
    if model is None:
        model = cfg.get("model")
    auto_open_conversation = __facade_binding(
        "_resolve_auto_open_conversation_from_config",
        _resolve_auto_open_conversation_from_config,
    )(cfg)

    # Validate option combinations before any side effects — see
    # the same comment in the claude command. _ensure_backend can
    # spawn the daemon and take the full local-server-discover
    # timeout to fail, which would make a bad arg pair look like
    # a backend outage instead of a usage error.
    choice = __facade_binding("_split_resume_value", _resume_split_resume_value)(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session and --resume are mutually exclusive; "
            "prefer --resume (--session is deprecated).",
        )

    # Ensure the host daemon (local when ``--server`` is omitted/empty,
    # remote otherwise) and resolve the concrete Omnigent server URL. Codex follows
    # the same ownership model as attach/run/claude: the daemon-spawned runner
    # owns the app-server and TUI; the CLI attaches to the tmux terminal.
    server = __facade_binding("_ensure_backend", _ensure_backend)(server)

    resolved_session_id = (
        choice.conversation_id if choice.conversation_id is not None else session_id
    )

    run_codex_native(
        server=server,
        session_id=resolved_session_id,
        resume_picker=choice.picker,
        codex_args=codex_args,
        model=model,
        prompt=prompt,
        auto_open_conversation=auto_open_conversation,
    )
