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
@click.argument("target", required=False, metavar="[AGENT]")
@click.option(
    "--tools",
    default=None,
    help="Client-side tool set name (e.g. 'coding') for shell access.",
)
@click.option("--harness", default=None, help=_RUN_HARNESS_HELP)
@click.option("--model", default=None, help=_MODEL_HELP)
@click.option("-p", "--prompt", default=None, help=_PROMPT_HELP)
@click.option("--system-prompt", "system_prompt", default=None, help=_SYSTEM_PROMPT_HELP)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=_RESUME_HELP,
)
@click.option(
    "-c", "--continue", "resume_latest", is_flag=True, default=False, help=_CONTINUE_HELP
)
@click.option("--fork", "fork_session_id", default=None, help=_FORK_HELP)
@click.option("--no-session", "ephemeral", is_flag=True, default=False, help=_NO_SESSION_HELP)
@click.option("--log/--no-log", "log", default=False, help=_LOG_HELP)
@click.option(
    "--server",
    default=None,
    help=(
        "Remote omnigent URL. Uploads the local YAML as an ephemeral "
        "agent, spawns a LOCAL runner that tunnels to this server (so "
        "terminals/MCPs run on your laptop), and connects the REPL to it. "
        'Pass --server "" to auto-spawn a persistent local server in the '
        "background and target that instead of a remote one."
    ),
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
@click.option(
    "--host",
    "register_host",
    is_flag=True,
    default=False,
    help=(
        "Register this machine as a host with the remote server "
        "(inline equivalent of `omnigent host`). Requires --server."
    ),
)
def run(
    target: str | None,
    tools: str | None,
    harness: str | None,
    model: str | None,
    prompt: str | None,
    system_prompt: str | None,
    resume: str | None,
    resume_latest: bool,
    fork_session_id: str | None,
    ephemeral: bool,
    log: bool,
    server: str | None,
    debug_events: bool,
    register_host: bool,
) -> None:
    """Start a session with an Omnigent agent.

    AGENT may be an agent YAML file or an agent directory. Without AGENT,
    pass ``--server`` to connect directly to a server, or pass
    ``--harness`` to launch a built-in harness directly.

    Default: omnigent server+REPL architecture (spawns a local
    server, REPL connects as an HTTP client). With ``--server <url>`` and
    no AGENT, connect directly to that server; with AGENT, use local
    runner + remote server topology (RUNNER.md §6 Flow 1) - laptop hosts
    runner/harnesses, server hosts state.

    \b
    Examples:
      omnigent run --harness claude-sdk
      omnigent run --harness codex -p "review the last commit"
      omnigent run examples/hello_world.yaml
      omnigent run examples/hello_world.yaml --harness codex --model gpt-5.4-mini
      omnigent run --server http://localhost:6767
      omnigent run examples/databricks_coding_agent.yaml --server https://<app>.databricksapps.com
    """
    # Apply config defaults for any value the user did not pass explicitly.
    # Explicit CLI args always take precedence; project-local config overrides
    # global config, which provides user-level defaults.
    server_source = click.get_current_context().get_parameter_source("server")
    server_from_cli = server_source is not None and server_source.name == "COMMANDLINE"
    harness_source = click.get_current_context().get_parameter_source("harness")
    harness_from_cli = harness_source is not None and harness_source.name == "COMMANDLINE"
    direct_server_cli = (
        target is None and server_from_cli and server is not None and not harness_from_cli
    )

    _global_cfg = _load_effective_config()
    if target is None and not direct_server_cli:
        # Harness-aware default-agent resolution (this branch) under main's
        # direct-`--server` guard: skip the configured default_agent when the
        # invocation is a bare `--server` (no AGENT, no --harness), else pick
        # it — but fall back to a built-in launcher when an explicit --harness
        # doesn't match the default agent's harness.
        target = _resolve_default_agent_target(_global_cfg.get("default_agent"), harness)
    if server is None:
        server = _global_cfg.get("server")
    if model is None and not direct_server_cli:
        model = _global_cfg.get("model")
    if harness is None and not direct_server_cli:
        harness = _global_cfg.get("harness")

    # First-run smart defaults: a bare `run` with no AGENT, no --harness, and no
    # explicit persisted default → derive a harness from the *current* creds
    # (Claude→polly, else Codex, else Pi); or drop into `configure harnesses`
    # when nothing is set up. The derived pick is NOT persisted, so it tracks
    # the credentials — adding Claude later promotes a Codex-only user to polly.
    if target is None and harness is None and not direct_server_cli:
        plan = _resolve_first_run_plan()
        if plan is None:
            return  # nothing configured even after offering configure — exit cleanly
        harness = plan.harness
        target = plan.agent  # polly path for Claude; None (bare harness) for codex/pi

    # Interactive ``omnigent run`` opens the live conversation in the
    # browser by default so users discover the web UI once the server is up
    # (the accounts-mode magic-redeem auto-open used to surface this, but
    # accounts is no longer the default auth). An explicit
    # ``auto_open_conversation`` config value (true/false) always wins, so
    # users who opted out stay opted out. Headless ``-p`` one-shots stay
    # quiet unless the user explicitly opted in.
    auto_open_setting = _resolve_auto_open_conversation_setting(_global_cfg)
    auto_open_conversation = auto_open_setting if auto_open_setting is not None else prompt is None

    # NOTE: the host daemon + Omnigent server are ensured inside ``run_chat``'s
    # non-URL branch (a URL ``target`` connects directly). ``--host`` is now
    # redundant (the daemon is always ensured) and kept only as a no-op.
    del register_host

    choice = _split_resume_value(resume)
    # Capture resume-safe CLI parts before dispatch mutates target,
    # harness, or model for no-AGENT launcher mode.
    resume_parts = _build_resume_parts()
    _dispatch_run(
        target=target,
        tools=tools,
        harness=harness,
        model=model,
        prompt=prompt,
        system_prompt=system_prompt,
        server=server,
        resume_picker=choice.picker,
        resume_latest=resume_latest,
        resume_conversation_id=choice.conversation_id,
        fork_session_id=fork_session_id,
        ephemeral=ephemeral,
        log=log,
        debug_events=debug_events,
        resume_parts=resume_parts,
        auto_open_conversation=auto_open_conversation,
        server_from_cli=server_from_cli,
    )


