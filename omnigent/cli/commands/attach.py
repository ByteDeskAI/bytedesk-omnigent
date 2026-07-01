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

@cli.command()
@click.argument("conversation", required=False, metavar="[CONVERSATION_ID]")
@click.option(
    "--server",
    default=None,
    help=(
        "AP server hosting the session. Defaults to the configured server, "
        "or a local server already running in the background."
    ),
)
@click.option(
    "--tools",
    default=None,
    help="Client-side tool set name (e.g. 'coding') for shell access.",
)
@click.option(
    "--debug-events",
    "debug_events",
    is_flag=True,
    default=False,
    help=(
        "Enable the SSE-to-UI debug pipeline: Ctrl+E event tape "
        "overlay, JSONL event log (~/.omnigent/debug/), and "
        "pipeline stage counters in the toolbar."
    ),
)
def attach(
    conversation: str | None,
    server: str | None,
    tools: str | None,
    debug_events: bool,
) -> None:
    """Attach the REPL to a LIVE session — never starts anything.

    ``attach`` is a thin client: it joins an already-running conversation
    on a server and streams its I/O. It never spawns a server, runner, or
    harness, applies no model/harness defaults, and errors loudly when
    there is nothing live to attach to. To START a session use
    ``omnigent run``; to reopen/restart a stored one use
    ``omnigent resume``.

    \b
    Examples:
      omnigent attach conv_abc123
      omnigent attach conv_abc123 --server https://<app>.databricksapps.com
    """
    cfg = _load_effective_config()
    base_url = _resolve_attach_server(server, cfg.get("server"))
    if base_url is None:
        raise click.ClickException(
            "No server to attach to. `attach` joins a LIVE session on a running "
            "server — start one with `omnigent run`, or point at one with "
            "`--server <url>`."
        )
    if conversation is None:
        raise click.ClickException(
            "Nothing to attach to: `attach` joins a LIVE session by id. "
            f"Run `omnigent host status` to list sessions on {base_url}, or "
            "`omnigent run <agent.yaml>` to start a new one."
        )
    _require_live_conversation(base_url=base_url, conversation_id=conversation)
    auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)
    from omnigent.chat import run_attach

    # Attach is a pure client: it joins the live session and dispatches turns to
    # the runner the host already bound (like the web UI co-drive), never
    # spawning a server/runner/harness. ``run_attach`` fails loud if the host
    # is offline (no online runner to dispatch to).
    run_attach(
        base_url=base_url,
        conversation_id=conversation,
        client_tools=tools,
        debug_events=debug_events,
        auto_open_conversation=auto_open_conversation,
        resume_parts=["cli", "attach", conversation, "--server", base_url],
    )


# `run` absorbs the legacy ``omnigent run`` subcommand. With an AGENT
# argument it opens the interactive REPL on a freshly started session;
# without AGENT it can launch a built-in harness directly via ``--harness``.
# Both paths route through the same Omnigent server+REPL dispatcher.
