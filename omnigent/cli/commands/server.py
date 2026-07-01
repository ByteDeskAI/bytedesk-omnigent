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

@cli.group("server", invoke_without_command=True)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host to bind to.",
)
@click.option(
    "--port",
    "-p",
    default=_DEFAULT_LOCAL_PORT,
    show_default=True,
    type=int,
    help="Port to listen on.",
)
@click.option(
    "--database-uri",
    default=None,
    help="Database URI for stores.  [default: sqlite at <data-dir>/chat.db, "
    "machine-global so `server` and `run` share one admin]",
)
@click.option(
    "--artifact-location",
    default=None,
    help="Path for artifact storage.  [default: <data-dir>/artifacts]",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to YAML config file.",
)
@click.option(
    "--execution-timeout",
    default=None,
    type=int,
    help="Max wall-clock seconds per agent execution.  [default: 7200]",
)
@click.option(
    "--agent",
    "agent_dirs",
    multiple=True,
    type=click.Path(exists=True),
    help=(
        "Pre-register an agent from a directory at startup. "
        "Can be repeated. If the agent name already exists, "
        "the bundle is replaced."
    ),
)
@click.option(
    "--open/--no-open",
    "auto_open",
    default=True,
    help=(
        "On first boot of accounts auth, open the magic-redeem URL in the "
        "user's browser so the web UI signs in without password entry. "
        "Default: --open. Pass --no-open for headless / SSH / Docker."
    ),
)
@click.option(
    "--admin-password",
    default=None,
    help=(
        "Set the first-run accounts admin password non-interactively "
        "(alternative to OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD). Only "
        "takes effect on the very first boot of a machine's accounts DB; "
        "ignored with a warning if an admin already exists."
    ),
)
@click.pass_context
def server(
    ctx: click.Context,
    host: str,
    port: int,
    database_uri: str | None,
    artifact_location: str | None,
    config_path: str | None,
    execution_timeout: int | None,
    agent_dirs: tuple[str, ...],
    auto_open: bool,
    admin_password: str | None,
) -> None:
    """Start the Omnigent server in the foreground, or manage the background server.

    Bare ``omnigent server`` runs the server in the FOREGROUND (Ctrl-C to
    stop) — for deploys / Docker. Subcommands manage the detached background
    server that ``run`` / ``claude`` / ``codex`` use: ``start`` (ensure it's
    up), ``stop`` (stop it and the local host daemon), ``status`` (is it up?).

    :param host: Interface to bind, e.g. ``"127.0.0.1"``.
    :param ctx: Click invocation context used to tell whether
        ``--port`` came from the command line or from the default.
    :param port: TCP port to listen on, e.g. ``6767``.
    :param database_uri: Optional database URI, e.g.
        ``"sqlite:///omnigent.db"``.
    :param artifact_location: Optional artifact location, e.g.
        ``"./artifacts"``.
    :param config_path: Optional YAML config file path.
    :param execution_timeout: Optional max agent execution seconds,
        e.g. ``7200``.
    :param agent_dirs: Agent directories or YAML files passed with
        ``--agent``.
    :param auto_open: Whether to open the magic-redeem URL in the
        user's browser on first boot of accounts mode. Translated
        into the ``OMNIGENT_ACCOUNTS_AUTO_OPEN`` env var so the
        lifespan startup hook (which actually fires the open after
        uvicorn binds) reads it without a kwarg threading change.
    :param admin_password: Optional first-run accounts admin password
        from ``--admin-password``, e.g. ``"hunter2"``. Folded into the
        ``OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`` env var that
        bootstrap reads; ``None`` leaves the env var untouched.
    :returns: None.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand (start/stop/status) handles this invocation; the body
        # below is the foreground-server path for the bare ``server`` group.
        return
    port_source = ctx.get_parameter_source("port")
    port_was_explicit = port_source is click.core.ParameterSource.COMMANDLINE
    if port_was_explicit:
        _assert_server_port_bindable(host, port)

    # --admin-password is sugar for the INIT_ADMIN_PASSWORD env var that
    # bootstrap_admin already consumes — fold it in here so the rest of
    # the startup path has a single source. setdefault so an explicit
    # env var wins over the flag (consistent with "explicit env wins").
    # Whether it actually takes effect (vs. being ignored with a warning
    # because an admin already exists) is decided in bootstrap_admin.
    if admin_password:
        os.environ.setdefault("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", admin_password)

    # Translate --no-open into the env var the lifespan hook reads.
    # We use an env var rather than threading the flag through
    # create_app so the same toggle works for callers (Docker
    # entrypoint, future `omnigent run`) that build the app
    # outside this CLI command.
    os.environ["OMNIGENT_ACCOUNTS_AUTO_OPEN"] = "1" if auto_open else "0"

    # Unified local-server lifecycle — applies ONLY to a *bare* loopback
    # `omnigent server` (default port + default DB + artifacts), i.e.
    # THE canonical machine-global local server recorded in
    # ~/.omnigent/local_server.pid:
    #   - If a healthy one is already running (started here OR spawned by
    #     the `run`/`host` daemon), reuse it — print its URL and exit
    #     instead of starting a competing second server on the shared DB.
    #   - Otherwise prefer the requested port (default 6767), falling back
    #     to a free one if taken, and register ourselves in the pidfile so
    #     the daemon reuses THIS server. (See host/local_server.py.)
    #
    # An explicit --port / --database-uri / --artifact-location means "be a
    # DEDICATED server here" — the daemon's own spawn (ensure_local_omnigent_server)
    # and the e2e harness both do this. Such a server must bind its requested
    # port and must NOT consult or register in the shared pidfile, or it would
    # reuse/hijack the canonical server and exit without ever binding its port.
    # Likewise a non-loopback bind (`--host 0.0.0.0`, a real deploy) is exempt
    # and binds the exact port.
    _is_canonical_local_server = (
        host in ("127.0.0.1", "localhost")
        and database_uri is None
        and artifact_location is None
        and not port_was_explicit
    )

    # Single-user marker: ANY loopback-bound `omnigent server` running
    # the env-unset header default IS a local single-user runtime — the
    # user's own machine, no proxy to inject identity — so it keeps the
    # no-login header-mode "local" fallback (same posture as the daemon
    # / `omnigent run` spawn paths, which set this var themselves). The
    # bind address is the discriminator, NOT the port/db-uri: a
    # dedicated `omnigent server --port 9001 --database-uri …` on
    # loopback (manual local runs, the e2e harness) is still single
    # user, so it must not 401 its own headerless traffic. What stays
    # fail-closed: a non-loopback bind (`--host 0.0.0.0`,
    # a network-exposed deploy — those MUST front a proxy or use
    # accounts/oidc) and an explicit OMNIGENT_AUTH_PROVIDER=header
    # deploy behind an identity-injecting proxy. setdefault so an
    # operator's explicit OMNIGENT_LOCAL_SINGLE_USER=0 wins. Must run
    # before create_auth_provider() below, which reads the var.
    from omnigent.server.auth import resolve_auth_source as _resolve_auth_source

    _is_loopback_bind = host in ("127.0.0.1", "localhost", "::1")
    # Compose-style deploys pass OMNIGENT_AUTH_PROVIDER as an empty
    # string when unset ("${VAR:-}"), so empty and missing both mean
    # "not explicitly pinned".
    _raw_auth_provider = os.environ.get("OMNIGENT_AUTH_PROVIDER")
    _auth_provider_explicit = bool(_raw_auth_provider and _raw_auth_provider.strip())
    if _is_loopback_bind and not _auth_provider_explicit and _resolve_auth_source() == "header":
        os.environ.setdefault("OMNIGENT_LOCAL_SINGLE_USER", "1")

    if _is_canonical_local_server:
        from omnigent.host.local_server import (
            local_server_url_if_healthy,
            pick_local_port,
        )

        _existing = local_server_url_if_healthy()
        if _existing is not None:
            click.echo(
                f"A local server is already running at {_existing} — reusing it.\n"
                "Stop it first if you want to start a fresh one "
                "(or pass --server <url> to target a different server)."
            )
            return
        _picked = pick_local_port(port)
        if _picked != port:
            click.echo(
                f"  ⚠ port {port} is busy — using {_picked} instead.",
                err=True,
            )
        port = _picked

    import uvicorn

    from omnigent.server.app import create_app
    from omnigent.server.auth import create_auth_provider
    from omnigent.server.server_config import config_str_list
    from omnigent.server.websocket_limits import CONTROL_WEBSOCKET_MAX_MESSAGE_BYTES
    cfg = _load_config(config_path)

    # CLI args take precedence over config file, which takes precedence
    # over defaults.
    db_uri = database_uri or cfg.get("database_uri", _default_db_uri())
    art_loc = artifact_location or cfg.get("artifact_location", _default_artifact_location())

    # Resolve relative artifact location against config file's directory
    # (only when the value came from the config file, not CLI).
    if config_path and artifact_location is None and _is_relative_artifact_location(art_loc):
        art_loc = str(Path(config_path).parent / art_loc)

    # SQLite won't create the DB file's parent dir; do it before any store
    # connects, else a fresh <data_dir> (first run, or a cleared dir) fails
    # with "unable to open database file".
    _ensure_sqlite_parent_dir(db_uri)

    # Build stores through the shared composition-root factory. AgentStore is
    # NATS-only after the cutover; the SQLAlchemy AgentStore class remains in
    # tree for verification but is not a runtime provider.
    from omnigent.stores.factory import StoreBootstrapper

    _stores = StoreBootstrapper.create(db_uri, art_loc)
    agent_store = _stores.agent_store
    file_store = _stores.file_store
    conversation_store = _stores.conversation_store
    comment_store = _stores.comment_store
    policy_store = _stores.policy_store
    permission_store = _stores.permission_store
    artifact_store = _stores.artifact_store
    _bootstrapped_host_store = _stores.host_store

    # Initialize the runtime with store references so workflow code
    # can access them via getter functions (get_agent_cache(), etc.).
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps

    agent_cache = AgentCache(
        artifact_store=artifact_store,
        cache_dir=Path(art_loc) / ".cache",
    )
    # CLI flag > config file > RuntimeCaps default (7200s = 2 hours).
    # 7200 matches RuntimeCaps.execution_timeout default.
    effective_timeout = execution_timeout or cfg.get("execution_timeout") or 7200

    from omnigent.spec import parse_default_policies, parse_server_llm

    caps = RuntimeCaps(
        execution_timeout=int(effective_timeout),
        default_policies=parse_default_policies(cfg.get("policies")),
        llm=parse_server_llm(cfg.get("llm")),
    )
    init_runtime(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
        policy_store=policy_store,
        caps=caps,
    )

    # Initialize OpenTelemetry observability. No-op when
    # OTEL_EXPORTER_OTLP_ENDPOINT is unset; see
    # designs/OBSERVABILITY.md for the env var reference.
    from omnigent.runtime import telemetry

    telemetry.init()

    # Sentry error + performance telemetry (BDP-2550). Opt-in: no-op unless
    # OMNIGENT_SENTRY_DSN is set. Tagged component=server.
    from omnigent.runtime.sentry import init_sentry

    init_sentry("server")

    # Read a pre-shared tunnel token from the environment if the
    # caller (e.g. _start_local_server) spawns the runner externally
    # and needs the server to accept exactly that runner's tunnel.
    # When unset the server accepts any token-bound runner
    # (runner_tunnel_tokens=None) — the standard posture for deployed
    # servers where runners authenticate via Databricks OAuth.
    _tunnel_token = os.environ.get("OMNIGENT_RUNNER_TUNNEL_TOKEN")
    _runner_tunnel_tokens: frozenset[str] | None = (
        frozenset({_tunnel_token}) if _tunnel_token else None
    )

    # Pre-register agents from --agent directories.
    for agent_dir in agent_dirs:
        _preregister_agent(
            Path(agent_dir),
            agent_store,
            artifact_store,
            agent_cache,
        )

    # Reuse the StoreBootstrapper-built host_store.
    host_store = _bootstrapped_host_store

    # Managed sandbox hosts (host_type="managed" sessions): parse the
    # config's `sandbox:` section up front so an operator typo stops
    # startup instead of 502-ing the first managed session.
    from omnigent.server.managed_hosts import parse_sandbox_config

    try:
        sandbox_config = parse_sandbox_config(cfg.get("sandbox"))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    # Accounts mode ergonomics: when accounts mode is selected
    # (OMNIGENT_AUTH_ENABLED=1 without OIDC config, or an explicit
    # OMNIGENT_AUTH_PROVIDER=accounts), supply sensible defaults
    # for the two vars they would otherwise have to set manually.
    # Both defaults respect operator overrides (setdefault, no
    # override clobber). We gate on the *resolved* selection (not
    # just "auth provider unset") so a bare header-mode local server
    # — the env-unset default — and an OIDC deploy don't mint accounts
    # secrets they never read.
    #
    # COOKIE_SECRET: persist in the artifact dir so sessions survive
    # restart. Operator-set value still wins for HA deploys.
    # BASE_URL: default to the CLI's bind+port so local dev "just
    # works". Docker / remote deploys behind a public domain still
    # set this explicitly.
    from omnigent.server.auth import resolve_auth_source

    if resolve_auth_source() == "accounts":
        from omnigent.server.accounts_secret import load_or_generate_cookie_secret

        os.environ.setdefault(
            "OMNIGENT_ACCOUNTS_COOKIE_SECRET",
            load_or_generate_cookie_secret(art_loc),
        )
        os.environ.setdefault("OMNIGENT_ACCOUNTS_BASE_URL", f"http://{host}:{port}")

    auth_provider = create_auth_provider()

    # Accounts mode: construct the AccountStore (sibling to PermissionStore)
    # here and pass it to create_app explicitly. Any deploy that doesn't run
    # accounts (the internal hosted product) passes account_store=None and
    # the entire accounts surface stays inactive.
    account_store = None
    from omnigent.server.auth import UnifiedAuthProvider as _UAP

    if isinstance(auth_provider, _UAP) and auth_provider._source == "accounts":
        from omnigent.server.accounts_store import SqlAlchemyAccountStore

        account_store = SqlAlchemyAccountStore(db_uri)

    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        comment_store=comment_store,
        policy_store=policy_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        runner_tunnel_tokens=_runner_tunnel_tokens,
        permission_store=permission_store,
        auth_provider=auth_provider,
        host_store=host_store,
        account_store=account_store,
        policy_modules=cfg.get("policy_modules"),
        admins=config_str_list(cfg.get("admins")),
        allowed_domains=config_str_list(cfg.get("allowed_domains")),
        sandbox_config=sandbox_config,
    )

    click.echo(f"Starting omnigent server on {host}:{port}")
    click.echo(f"  database:  {db_uri}")
    click.echo(f"  artifacts: {art_loc}")
    # A foreground server streams uvicorn logs to this terminal, but the
    # always-on diagnostics (omnigent.* loggers, captured warnings) also land
    # in a persistent per-invocation file — point at it so there's a concrete
    # log to grep after the terminal scrolls. None only in the detached spawn
    # path (`-m omnigent.cli server`, no setup_cli_logging), whose captured
    # log `server start` already reports.
    from omnigent.cli_diagnostics import current_cli_log_path

    _cli_log = current_cli_log_path()
    if _cli_log is not None:
        click.echo(f"  log:       {_display_path(_cli_log)}")

    # First-run terminal setup: the FALLBACK entry point. Fires only on
    # an interactive TTY when no admin exists AND the browser isn't about
    # to open the web Create-admin form (i.e. --no-open, or a non-loopback
    # base URL). The default `omnigent server` on loopback opens the
    # browser to the form instead, so this no-ops there. (The other entry
    # points are --admin-password and the web form.)
    _maybe_prompt_first_admin(account_store, auth_provider, auto_open=auto_open)

    # Warn loudly when the SPA bundle is absent: the server still boots
    # but serves an API-only JSON landing at "/", so the operator hits
    # http://host:port expecting the web UI and gets JSON with no clue
    # why. The bundle is npm-build output (not tracked in git); a dev
    # checkout that never ran `npm run build` has an empty static dir.
    from omnigent.server.app import _WEB_UI_DIST

    if not (_WEB_UI_DIST / "index.html").is_file():
        click.echo(
            "  ⚠ web UI not built — serving API only. "
            "Run `cd ap-web && npm install && npm run build`, "
            "then restart (or install a release wheel/image).",
            err=True,
        )

    # Advertise this server in the shared pidfile so the run/host
    # daemon discovers and reuses it (loopback only). Cleared on exit so
    # a clean shutdown doesn't leave a stale record.
    if _is_canonical_local_server:
        from omnigent.host.local_server import (
            clear_local_server_record,
            register_local_server,
        )

        # Stamp the same config signature host/run compute so they reuse
        # this foreground server instead of tearing it down on a spurious
        # sig mismatch.
        register_local_server(port)
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_config=_server_uvicorn_log_config(),
            ws_max_size=CONTROL_WEBSOCKET_MAX_MESSAGE_BYTES,
            timeout_graceful_shutdown=_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        )
    finally:
        if _is_canonical_local_server:
            clear_local_server_record()


def _stop_local_server_and_daemon(*, force: bool) -> bool:
    """Stop the background Omnigent server and the local host daemon that owns it.

    Stops the local-mode host daemon first (the daemon spawns its server
    once and never respawns it, so leaving it alive would only have it
    reconnect-flap against a dead server), then the detached Omnigent server
    recorded in ``~/.omnigent/local_server.pid``. Best-effort and
    idempotent — a missing daemon or server is a no-op.

    :param force: SIGKILL the daemon after the grace period if it does not
        exit on SIGTERM.
    :returns: ``True`` if a healthy background server was running when
        called, ``False`` otherwise.
    """
    was_running = local_server_url_if_healthy() is not None
    local_record = _find_daemon_record(_LOCAL_DAEMON_MARKER)
    if local_record is not None:
        # A stubborn daemon shouldn't block stopping the server.
        with contextlib.suppress(click.ClickException):
            _terminate_daemon(local_record, force=force)
    stop_local_omnigent_server()
    # Also catch an orphan on the canonical port whose pidfile was lost, so
    # `server stop` isn't blind to it (it reported "No background server is
    # running" while one was still listening on the default port).
    orphan_pid = stop_untracked_local_server()
    return was_running or orphan_pid is not None


@server.command("start")
def server_start() -> None:
    """Ensure the managed background Omnigent server is running.

    Reuses a healthy background server if one is already up (started here or
    by a prior ``run`` / ``host``); otherwise spawns a detached one on a
    free loopback port and prints its URL. The background counterpart to the
    foreground bare ``omnigent server``.

    :returns: None.
    """
    startup = ensure_local_omnigent_server()
    verb = (
        "Started background server at"
        if startup.spawned
        else "Background server already running at"
    )
    click.echo(f"{verb} {startup.url}")
    # Surface the exact log file so a detached server isn't a black box —
    # `server start` is otherwise the only signal it ever emits. Known for a
    # spawned server and (via the log-path sidecar) for a reused one too;
    # absent only for a foreground `omnigent server` whose logs stream to
    # its own terminal.
    if startup.log_path is not None:
        click.echo(f"  log: {_display_path(startup.log_path)}")


@server.command("stop")
@click.option(
    "--force",
    is_flag=True,
    help="SIGKILL the local host daemon if it does not exit on SIGTERM.",
)
def server_stop(force: bool) -> None:
    """Stop the background Omnigent server and the local host daemon.

    Stops the local host daemon first, then the detached server recorded
    in ``~/.omnigent/local_server.pid`` — its web UI and sessions become
    unreachable. To stop hosting but KEEP the server up, use
    ``omnigent host stop``; to stop everything, use ``omnigent stop``.

    :param force: SIGKILL the local host daemon after the grace period if it
        does not exit on SIGTERM.
    :returns: None.
    """
    if _stop_local_server_and_daemon(force=force):
        click.echo("Stopped the background server.")
    else:
        click.echo("No background server is running.")


@server.command("status")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def server_status(json_output: bool) -> None:
    """Show whether the background Omnigent server is running.

    Reports the recorded pid/port, URL, live-session count, and whether a
    local host daemon is attached. Reads ``~/.omnigent/local_server.pid``
    and probes ``/health``.

    :param json_output: Emit machine-readable JSON instead of text.
    :returns: None.
    """
    info = local_server_status()
    daemon_attached = _find_daemon_record(_LOCAL_DAEMON_MARKER) is not None
    sessions: int | None = None
    if info.running and info.url is not None:
        # Session count crosses the HTTP boundary; a transient failure
        # shouldn't break `status`, so leave the count unknown instead.
        with contextlib.suppress(click.ClickException):
            pages = _fetch_session_pages(base_url=info.url, connected_only=True)
            sessions = len(pages.sessions)
    if json_output:
        click.echo(
            json.dumps(
                {
                    "running": info.running,
                    "pid": info.pid,
                    "port": info.port,
                    "url": info.url,
                    "log_path": str(info.log_path) if info.log_path else None,
                    "live_sessions": sessions,
                    "daemon_attached": daemon_attached,
                },
                indent=2,
            )
        )
        return
    if not info.running:
        click.echo("Background server: not running.")
        return
    click.echo(f"Background server: running at {info.url} (pid {info.pid}, port {info.port})")
    if info.log_path is not None:
        click.echo(f"  log: {_display_path(info.log_path)}")
    if sessions is not None:
        click.echo(f"  live sessions: {sessions}")
    click.echo(f"  host daemon attached: {'yes' if daemon_attached else 'no'}")


