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


def __facade_binding(name: str, fallback):
    import omnigent.cli as cli_facade

    return getattr(cli_facade, name, fallback)


def _resume_workspace_api_server_url(server: str) -> str:
    from .debug import _workspace_api_server_url as fallback

    workspace_api_server_url = __facade_binding("_workspace_api_server_url", fallback)
    return workspace_api_server_url(server)


@cli.command()
@click.argument("target", required=False, metavar="[CONV_ID]")
@click.option(
    "--server",
    default=None,
    help=(
        "Remote omnigent URL. When set, the picker / lookup queries "
        "this server instead of starting a local one. Required when "
        "running ``omnigent resume`` without a conversation id."
    ),
)
def resume(
    target: str | None,
    server: str | None,
) -> None:
    # Click uses the docstring as --help text — keep param docs in
    # comments so they don't leak into CLI output.
    #
    # :param target: Optional Omnigent conversation id, e.g.
    #     ``"conv_abc123"``. None falls through to the picker.
    # :param server: Remote Omnigent server URL (optional in id mode;
    #     required in picker mode).
    """Resume an Omnigent conversation, auto-dispatching by runtime.

    \b
    With CONV_ID: looks up the conversation and dispatches to the
    matching wrapper. claude-native sessions land in
    ``omnigent claude``; everything else surfaces a clear hint to
    use ``omnigent run --resume <id> <agent.yaml>``.

    \b
    Without CONV_ID: opens a cross-agent picker over your prior
    conversations (requires ``--server``). Dispatch follows from
    the row you select.

    \b
    Examples:
      omnigent resume conv_abc123
      omnigent resume conv_abc123 --server https://<app>.databricksapps.com
      omnigent resume --server https://<app>.databricksapps.com
    """
    from omnigent.resume_dispatch import run_resume

    run_resume(
        target=target,
        # A bare Databricks workspace URL means its /api/2.0/omnigent mount.
        server=_resume_workspace_api_server_url(server) if server else server,
    )


# Shared option help for ``run`` and the harness commands. These are the same
# flags the legacy argparse CLI exposed — keeping them on the unified
# click CLI so users don't regress when a YAML declares no executor
# block (e.g. ``examples/hello_world.yaml``) or when they want to
# choose model/harness without editing the agent file. See
# ``omnigent.chat.run_chat`` for how local-agent options get baked
# into a materialized copy of the spec before the server starts.
_HARNESS_CHOICES_HELP = (
    "'claude' (alias for 'claude-sdk'), 'claude-sdk', 'codex', "
    "'cursor', "
    "'openai-agents', 'open-responses', or 'pi'"
)
_HARNESS_HELP = f"Harness to use for a local agent: {_HARNESS_CHOICES_HELP}."
_RUN_HARNESS_HELP = (
    f"Harness to use: {_HARNESS_CHOICES_HELP}. Without AGENT, launches that harness directly."
)
_MODEL_HELP = "Model to use for the agent."
_PROMPT_HELP = "Send this as the first message when the REPL starts."
_SYSTEM_PROMPT_HELP = "Instructions to use for the agent."
_RESUME_HELP = (
    "Resume a prior conversation. With no value, opens an interactive "
    "picker; with a conversation id (e.g. --resume conv_abc123), attaches "
    "directly to that conversation."
)
_CONTINUE_HELP = "Continue the most recent conversation for this agent."
_NO_SESSION_HELP = "Use a fresh temporary local session store for this run."

_FORK_HELP = "Fork an existing session by id and open the REPL on the fork."
_LOG_HELP = "Write a JSON dump of the conversation to ~/.omnigent/logs/ on exit."


_DEFAULT_HARNESS_PROMPTS = {
    "claude-sdk": (
        "You are Claude Code, running through Omnigent. "
        "Help the user with software engineering tasks."
    ),
    "codex": (
        "You are Codex, running through Omnigent. Help the user with software engineering tasks."
    ),
    "cursor": (
        "You are Cursor, running through Omnigent. Help the user with software engineering tasks."
    ),
}
_DEFAULT_HARNESS_PROMPT = "You are a helpful coding agent running through Omnigent."

# Harnesses whose auto-generated launcher YAML should include an
# ``os_env`` block.  This triggers the workflow's ``ToolManager``
# to inject ``sys_os_*`` tools into the request so file/shell
# operations route through the Omnigent dispatch path (runner
# visibility, timeouts, error recovery) instead of the harness's
# internal built-in tools.
_OS_ENV_HARNESSES: frozenset[str] = frozenset({"claude-sdk", "codex", "pi"})


def _validate_harness(harness: str) -> None:
    """
    Fail fast when *harness* is not a supported Omnigent harness.

    :param harness: Harness id from ``--harness``, e.g.
        ``"claude-sdk"``.
    :raises click.ClickException: If *harness* is unsupported.
    """
    from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

    if canonicalize_harness(harness) in OMNIGENT_HARNESSES:
        return
    allowed = ", ".join(sorted(OMNIGENT_HARNESSES))
    raise click.ClickException(f"Unsupported harness {harness!r}. Expected one of: {allowed}.")


def _default_harness_prompt(harness: str) -> str:
    """
    Return the lightweight generated-agent instructions for *harness*.

    :param harness: Supported harness id.
    :returns: Prompt text for the generated Omnigent YAML.
    """
    return _DEFAULT_HARNESS_PROMPTS.get(harness, _DEFAULT_HARNESS_PROMPT)


def _materialize_harness_launcher_file(
    *,
    harness: str,
    model: str | None,
    system_prompt: str | None,
) -> Path:
    """
    Create a temporary standalone Omnigent YAML for no-AGENT ``run``.

    The generated file uses the single-file Omnigent YAML shape
    (``name`` / ``prompt`` / ``executor``), not native AP
    ``config.yaml``. Passing this file to ``run_chat`` exercises the
    same compat adapter as ``omnigent run examples/foo.yaml``.

    Harnesses listed in :data:`_OS_ENV_HARNESSES` get an ``os_env``
    block so the workflow injects ``sys_os_*`` tools into the
    request — routing file/shell operations through the Omnigent
    dispatch path rather than the harness's internal built-ins.

    :param harness: Supported harness id to launch, e.g.
        ``"claude-sdk"``.
    :param model: Optional model value to bake into ``executor``.
    :param system_prompt: Optional instructions text to use as the
        YAML's top-level ``prompt``.
    :returns: Path to the generated ``*.yaml`` file.
    :raises click.ClickException: If *harness* is unsupported.
    """
    _validate_harness(harness)
    display_name = harness
    harness = canonicalize_harness(harness) or harness

    tmpdir = Path(tempfile.mkdtemp(prefix="omnigent-harness-launcher-"))
    yaml_path = tmpdir / f"{harness}.yaml"

    executor: dict[str, str] = {"harness": harness}
    if model is not None:
        executor["model"] = model

    raw = {
        "name": display_name,
        "prompt": system_prompt or _default_harness_prompt(harness),
        "executor": executor,
    }
    if harness in _OS_ENV_HARNESSES:
        raw["os_env"] = {"type": "caller_process", "sandbox": {"type": "none"}}
    yaml_path.write_text(yaml.safe_dump(raw, default_flow_style=False))
    return yaml_path


def _missing_run_agent_message() -> str:
    """Return the no-AGENT ``run`` guidance shown on missing input."""
    return (
        "Provide an AGENT path, pass --server to connect to a server, "
        "or pass --harness to launch a built-in "
        "harness directly:\n"
        "  omnigent run examples/hello_world.yaml\n"
        "  omnigent run --server http://localhost:6767\n"
        "  omnigent run --harness claude-sdk\n"
        "  omnigent run --harness codex"
    )


@dataclass(frozen=True)
class _ResumeChoice:
    """
    Outcome of parsing the click ``--resume`` option value.

    Named fields rather than a tuple so a future shape change (e.g. a
    third resume mode) doesn't become a positional break at every
    call site.
    """

    picker: bool
    conversation_id: str | None


def _split_resume_value(resume: str | None) -> _ResumeChoice:
    """
    Translate the click ``--resume`` option value into the internal
    ``resume_picker`` / ``resume_conversation_id`` shape.

    ``--resume`` is wired with ``is_flag=False`` + ``flag_value``, so
    click hands us one of three values:

    - ``None`` — option absent. No resume requested.
    - :data:`_RESUME_PICKER_SENTINEL` — ``--resume`` passed without a
      value. User wants the interactive picker.
    - any other string — ``--resume <id>``. User wants to attach to
      that specific conversation id.

    The downstream dispatcher / ``run_chat`` boundary still takes the
    two-field shape (the picker mode and the conv-id mode end up in
    different code paths inside ``_resolve_resume_target``); the
    split lives here so the click layer is the only place that knows
    about the consolidation.
    """
    if resume is None:
        return _ResumeChoice(picker=False, conversation_id=None)
    if resume == _RESUME_PICKER_SENTINEL:
        return _ResumeChoice(picker=True, conversation_id=None)
    return _ResumeChoice(picker=False, conversation_id=resume)


# Params that are one-shot or replaced on resume — excluded from the
# resume command hint.  Everything else Click parsed is preserved
# automatically, so new flags don't need any resume-hint bookkeeping.
_RESUME_SKIP_PARAMS: frozenset[str] = frozenset(
    {
        "prompt",
        "resume",
        "resume_latest",
        "fork_session_id",
        # ephemeral is session-scoped infrastructure flag, not
        # meaningful across invocations.
        "ephemeral",
    }
)


def _build_resume_parts() -> list[str]:
    """Build the flag-preserving prefix for the resume command from Click's
    parsed context.

    Iterates the active Click context's parameters and reconstructs
    every flag/argument whose value differs from its default, skipping
    one-shot params (``-p``, ``--fork``, ``-c``, ``--resume``, etc.).
    The caller appends ``--resume <conversation_id>`` and joins with
    :func:`shlex.join`.

    Must be called while a Click context is active (i.e. inside a
    Click command handler or a function it calls synchronously).

    :returns: Argument list prefix, e.g.
        ``["omnigent", "run", "agent.yaml", "--server",
        "https://example.com"]``.
    """
    ctx = click.get_current_context()
    parts: list[str] = ctx.command_path.split()

    for param in ctx.command.params:
        if param.name is None or param.name in _RESUME_SKIP_PARAMS:
            continue
        value = ctx.params.get(param.name)
        if value is None or value == param.default:
            continue

        if isinstance(param, click.Argument):
            parts.append(str(value))
        elif isinstance(param, click.Option):
            # Prefer the long-form flag (e.g. --harness over -h).
            flag = max(param.opts, key=len)
            if param.is_flag:
                parts.append(flag)
            else:
                parts.append(flag)
                parts.append(str(value))

    return parts


def _dispatch_run(
    *,
    target: str | None,
    tools: str | None,
    harness: str | None,
    model: str | None,
    prompt: str | None,
    system_prompt: str | None,
    server: str | None = None,
    resume_picker: bool = False,
    resume_latest: bool = False,
    resume_conversation_id: str | None = None,
    fork_session_id: str | None = None,
    ephemeral: bool = False,
    log: bool = False,
    debug_events: bool = False,
    resume_parts: list[str] | None = None,
    auto_open_conversation: bool = False,
    server_from_cli: bool = False,
) -> None:
    """
    Route ``omnigent run`` to the right impl.

    The click path always drives the Omnigent server-backed REPL. With
    ``--server <url>``, use that server URL instead of starting a
    local server. (``omnigent attach`` is a separate attach-only
    client and does NOT route through here.)

    :param target: Agent YAML/directory path, or ``None`` for
        ``run --harness ...`` launcher mode / ``--server`` direct-server
        mode.
    :param tools: ``--tools`` client-side tool set name.
    :param harness: ``--harness`` value.
    :param model: ``--model`` value.
    :param prompt: ``-p`` / ``--prompt`` value.
    :param system_prompt: ``--system-prompt`` value.
    :param server: Server URL from ``--server`` or config. With a local
        target, this is the Omnigent server used for upload/session setup; with
        no target and explicit ``--server``, this is the direct server.
    :param resume_picker: True when ``--resume`` / ``-r`` is set with
        no value (interactive picker).
    :param resume_latest: True when ``--continue`` / ``-c`` is set.
    :param resume_conversation_id: Explicit conversation id from
        ``--resume <id>``.
    :param fork_session_id: When set, fork this session and open the
        REPL on the fork. Mutually exclusive with ``--resume`` and
        ``--continue``.
    :param ephemeral: True when ``--no-session`` is set.
    :param log: True when ``--log`` is set.
    :param debug_events: True when ``--debug-events`` is set.
        Enables the SSE event tape overlay, JSONL event logging,
        and pipeline counters in the toolbar.
    :param resume_parts: Pre-built argument list prefix for the
        resume command shown on exit, e.g.
        ``["omnigent", "run", "agent.yaml", "--harness", "codex"]``.
        ``None`` when called outside the Click command path.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    :param server_from_cli: ``True`` when ``--server`` was explicitly
        provided on the command line. Used to distinguish direct-server
        mode from a configured default server.
    """
    if target is not None and _is_server_url(target):
        raise click.ClickException(
            "Server URLs are no longer accepted as the AGENT argument. "
            f"Use `omnigent run --server {target}` instead."
        )

    if target is None:
        if server_from_cli and server is not None and harness is None:
            base_url = server.rstrip("/")
            # Direct ``--server`` (no AGENT) has no local runner to bind, so an
            # interactive resume-by-id is an ATTACH: route it through the
            # `attach` pair (`_require_live_conversation` + `run_attach`), not
            # the picker+create path that crashed at runner-bind ("requires a
            # registered runner id"). Only the *pure interactive*
            # shape reroutes — a one-shot ``-p`` or any local-agent-only flag
            # (--model/--system-prompt/--log/--no-session) falls through to the
            # existing remote-URL path below, which one-shots or fails loud as
            # before instead of silently no-op'ing here. Picker/`--continue`
            # have no id to attach to and likewise stay on that path.
            # Pure interactive shape = no one-shot prompt and no local-agent-only
            # override; the ``resume_conversation_id is not None`` check stays in
            # the ``if`` so the type narrows for the calls below.
            is_interactive_shape = (
                prompt is None
                and not resume_latest
                and not resume_picker
                and fork_session_id is None
                and not log
                and not ephemeral
                and model is None
                and system_prompt is None
            )
            if resume_conversation_id is not None and is_interactive_shape:
                from omnigent.chat import _redirect_native_resume_if_needed, run_attach

                if _redirect_native_resume_if_needed(
                    base_url=base_url,
                    conversation_id=resume_conversation_id,
                    auto_open_conversation=auto_open_conversation,
                ):
                    return

                _require_live_conversation(
                    base_url=base_url,
                    conversation_id=resume_conversation_id,
                )
                run_attach(
                    base_url=base_url,
                    conversation_id=resume_conversation_id,
                    client_tools=tools,
                    debug_events=debug_events,
                    auto_open_conversation=auto_open_conversation,
                    # Keep the run-style parts so the exit "Resume:" hint
                    # reproduces the (now-working) command the user ran.
                    resume_parts=resume_parts,
                )
                return

            from omnigent.chat import run_chat

            run_chat(
                target=base_url,
                client_tools=tools,
                server_url=None,
                harness=harness,
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                ephemeral=ephemeral,
                resume_conversation_id=resume_conversation_id,
                resume_latest=resume_latest,
                resume_picker=resume_picker,
                fork_session_id=fork_session_id,
                log=log,
                debug_events=debug_events,
                resume_parts=resume_parts,
                auto_open_conversation=auto_open_conversation,
            )
            return
        if harness is None:
            raise click.ClickException(_missing_run_agent_message())
        if ephemeral:
            raise click.ClickException(
                "--no-session requires an AGENT path; no-AGENT harness launch "
                "already uses a generated temporary agent spec."
            )
        target = str(
            _materialize_harness_launcher_file(
                harness=harness,
                model=model,
                system_prompt=system_prompt,
            )
        )
        harness = None
        model = None
        system_prompt = None
    elif harness is not None:
        _validate_harness(harness)

    if server is not None:
        if _is_server_url(target):
            raise click.ClickException(
                "--server is for binding a LOCAL agent YAML to a remote "
                "server. Pass a YAML path as the target (got a URL)."
            )

    if fork_session_id is not None:
        if resume_conversation_id or resume_latest or resume_picker:
            raise click.ClickException(
                "--fork is mutually exclusive with --resume and --continue."
            )
        if prompt is not None:
            raise click.ClickException(
                "--fork requires interactive REPL mode; remove -p/--prompt."
            )

    harness = canonicalize_harness(harness)
    if prompt is not None:
        if resume_conversation_id is not None or resume_latest or resume_picker:
            from omnigent.chat import run_chat

            run_chat(
                target=target,
                client_tools=tools,
                server_url=server,
                harness=harness,
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                ephemeral=ephemeral,
                resume_conversation_id=resume_conversation_id,
                resume_latest=resume_latest,
                resume_picker=resume_picker,
                debug_events=debug_events,
                auto_open_conversation=auto_open_conversation,
            )
            return
        if log:
            raise click.ClickException(
                "--log is only supported in interactive REPL mode on this CLI path; "
                "remove -p/--prompt to run headlessly."
            )
        # Headless ``-p`` runs against the daemon-backed server too (the
        # host daemon connects to ``--server`` or starts a local server),
        # so it stays consistent with interactive mode. ``run_chat`` runs
        # one-shot and exits when ``initial_message`` is set. The only
        # exception is ``--no-session``: it keeps the legacy in-process
        # ephemeral path via ``run_prompt`` (no daemon, no persistence).
        if not ephemeral:
            from omnigent.chat import run_chat

            run_chat(
                target=target,
                client_tools=tools,
                server_url=server,
                harness=harness,
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                ephemeral=False,
                debug_events=debug_events,
                auto_open_conversation=auto_open_conversation,
            )
            return

        from omnigent.chat import run_prompt

        run_prompt(
            target=target,
            client_tools=tools,
            harness=harness,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            ephemeral=ephemeral,
        )
        return

    from omnigent.chat import run_chat

    run_chat(
        target=target,
        client_tools=tools,
        server_url=server,
        harness=harness,
        model=model,
        prompt=None,
        system_prompt=system_prompt,
        ephemeral=ephemeral,
        resume_conversation_id=resume_conversation_id,
        resume_latest=resume_latest,
        resume_picker=resume_picker,
        fork_session_id=fork_session_id,
        log=log,
        debug_events=debug_events,
        resume_parts=resume_parts,
        auto_open_conversation=auto_open_conversation,
    )


def _resolve_attach_server(server: str | None, configured_server: str | None) -> str | None:
    """
    Resolve the Omnigent server URL ``attach`` should join.

    Resolution order: an explicit ``--server`` value, then the configured
    ``server`` default, then a local Omnigent server already running in the
    background. ``attach`` never starts a server, so this returns ``None``
    when none of those is available and the caller fails loud.

    :param server: Explicit ``--server`` value, e.g.
        ``"https://example.databricksapps.com"``, or ``None``.
    :param configured_server: The ``server`` default from config (the
        ``server`` key of the effective merged config), or ``None``.
    :returns: Normalized base URL without a trailing slash, or ``None``.
    """
    chosen = server if server is not None else configured_server
    if chosen:
        # A bare Databricks workspace URL means its /api/2.0/omnigent mount.
        return _resume_workspace_api_server_url(chosen.rstrip("/"))
    local = local_server_url_if_healthy()
    return local.rstrip("/") if local else None


def _require_live_conversation(
    *,
    base_url: str,
    conversation_id: str,
) -> None:
    """
    Fail loud unless *conversation_id* is reachable on *base_url*.

    ``attach`` is an attach-only client; if the session is not live there
    is nothing to join, so we surface a clear error rather than letting the
    REPL connect to a phantom conversation. Issues a single
    ``GET /v1/sessions/{id}`` and raises on a transport failure or any
    non-200 status.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param conversation_id: Conversation id to attach to, e.g.
        ``"conv_abc123"``.
    :raises click.ClickException: When the server is unreachable or the
        conversation does not exist.
    """
    result = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/sessions/{conversation_id}",
    )
    # ``_host_http_json`` reports transport failures as status 0 (never
    # raises), so the server-down and missing-session cases both land here.
    if result.status_code == 0:
        raise click.ClickException(
            f"Couldn't reach a server at {base_url}: {_host_error_text(result.body)}. "
            "`attach` never starts a server — check the URL, or start one with "
            "`omnigent run`."
        )
    if result.status_code != 200:
        raise click.ClickException(
            f"No live session '{conversation_id}' on {base_url} "
            f"(server returned {result.status_code}). Run `omnigent host status` "
            "to list live sessions, or `omnigent run <agent.yaml>` to start one."
        )

