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

@cli.group("host", cls=_HostGroup, invoke_without_command=True)
@click.option("--server", default=None, help="Remote omnigent server URL.")
@click.pass_context
def host(ctx: click.Context, server: str | None) -> None:
    """
    Register this machine as a host with a server.

    \b
    Examples:
      omnigent host https://omnigent-app.databricksapps.com
      omnigent host --server https://omnigent-app.databricksapps.com
      omnigent host ""   # spawn + connect to a local server

    The server URL may be given positionally (``omnigent host
    <url>``) or via ``--server <url>``. A leading ``status``, ``stop``,
    or ``stop-session`` token still runs that management subcommand.

    :param ctx: Click invocation context. ``ctx.invoked_subcommand`` is
        set when a management subcommand such as ``"status"`` is running.
    :param server: Remote Omnigent server URL, e.g.
        ``"https://example.databricksapps.com"``. ``None`` falls back
        to config; empty string selects local mode.
    """
    ctx.ensure_object(dict)
    ctx.obj["server"] = server
    if ctx.invoked_subcommand is not None:
        return
    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")
    if server:
        # A bare Databricks workspace URL means its /api/2.0/omnigent mount.
        server = _workspace_api_server_url(server)

    from omnigent.host.connect import run_host_process

    # ``host`` IS the daemon (foreground). With no server URL, start (or
    # reuse) the local Omnigent server here and connect to it; otherwise connect to
    # the given remote/local URL. Unlike the background commands, we do not
    # spawn a second daemon via ``_ensure_host_daemon``.
    target = _normalize_daemon_target(server)
    # Only true when THIS invocation started the local server (vs reusing one
    # already started by `omnigent server` or a prior host/run daemon) —
    # gates the Ctrl-C stop-server prompt so we never offer to stop a server
    # we didn't bring up.
    spawned_local_server = False
    if not server:
        startup = ensure_local_omnigent_server()
        server = startup.url
        spawned_local_server = startup.spawned
    record = _foreground_daemon_record(
        target=target,
        server_url=server,
        host_id=_load_or_create_host_id(),
    )
    previous = _claim_foreground_daemon_record(record)
    # Only offer to stop the local server after a clean stop (Ctrl-C / normal
    # exit). A connection failure (SystemExit) leaves this False so we don't
    # prompt over an error.
    stopped_cleanly = False
    try:
        run_host_process(server_url=server)
        stopped_cleanly = True
    except KeyboardInterrupt:
        # Ctrl-C is the normal way to stop the foreground daemon — swallow it
        # so we can prompt below instead of exiting with an "Aborted!" trace.
        stopped_cleanly = True
    finally:
        _restore_replaced_daemon_record(record, previous)
        # Offer to stop the local server only when WE spawned it this run.
        # Not in --server mode (someone else's server), and not when we reused
        # a server started by `omnigent server` or another daemon — killing
        # that would surprise the user who brought it up independently. Users
        # expect Ctrl-C to stop "everything" they started, so the server we
        # spawned is fair game.
        if stopped_cleanly and spawned_local_server:
            _prompt_stop_local_server()


def _host_group_option(ctx: click.Context, key: str) -> str | None:
    """
    Read a group-level ``omnigent host`` option for a subcommand.

    :param ctx: Click context passed to a host subcommand.
    :param key: Group option key, e.g. ``"server"``.
    :returns: The string option value, or ``None``.
    """
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _resolve_host_server(server: str | None) -> str | None:
    """
    Resolve a host-management server from CLI or config.

    :param server: Explicit ``--server`` value, e.g.
        ``"https://example.databricksapps.com"``. ``None`` falls back
        to config; empty string selects local mode.
    :returns: Normalized server URL, or ``None`` for local mode.
    """
    if server is None:
        configured = _load_effective_config().get("server")
        server = str(configured) if configured else None
    # A bare Databricks workspace URL means its /api/2.0/omnigent mount.
    return _workspace_api_server_url(server.rstrip("/")) if server else None


def _daemon_base_url(record: _HostDaemonRecord) -> str | None:
    """
    Resolve the Omnigent server URL for a daemon record.

    :param record: Daemon registry record to inspect.
    :returns: Omnigent server URL, e.g. ``"http://127.0.0.1:8123"``, or
        ``None`` when a local daemon's server cannot be discovered.
    """
    if record.mode == "local":
        if record.resolved_server_url:
            return record.resolved_server_url.rstrip("/")
        local_url = local_server_url_if_healthy()
        return local_url.rstrip("/") if local_url else None
    return (record.server_url or record.target).rstrip("/")


def _selected_daemon_records(
    *,
    server: str | None,
    all_targets: bool,
    default_all: bool,
) -> list[_HostDaemonRecord]:
    """
    Select daemon records for a host-management command.

    :param server: Explicit ``--server`` value, e.g.
        ``"https://example.databricksapps.com"``. ``None`` may mean
        all targets or config/local depending on ``default_all``.
    :param all_targets: Whether ``--all`` was passed.
    :param default_all: Whether no selector should mean all records.
    :returns: Matching daemon records.
    :raises click.ClickException: If ``--server`` and ``--all`` conflict.
    """
    if all_targets and server is not None:
        raise click.ClickException("Use either --server or --all, not both.")
    if all_targets or (server is None and default_all):
        return _list_daemon_records()
    target = _normalize_daemon_target(_resolve_host_server(server))
    record = _find_daemon_record(target)
    return [] if record is None else [record]


def _host_http_json(
    *,
    base_url: str,
    method: str,
    path: str,
    params: dict[str, str | int] | None = None,
    json_body: _HostJsonObject | None = None,
    timeout_s: float = 10.0,
) -> _HostHttpResult:
    """
    Send one management request to an Omnigent server.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param method: HTTP method, e.g. ``"GET"`` or ``"POST"``.
    :param path: Request path beginning with ``/``, e.g.
        ``"/v1/hosts/host_abc"``.
    :param params: Optional query parameters, e.g. ``{"limit": 1000}``.
    :param json_body: Optional JSON body, e.g.
        ``{"type": "stop_session", "data": {}}``.
    :param timeout_s: Request timeout in seconds, e.g. ``2.0`` for a
        quick liveness probe. Defaults to ``10.0`` for management calls.
    :returns: Decoded HTTP result.
    """
    import httpx

    from omnigent.chat import _remote_headers

    try:
        with httpx.Client(
            base_url=base_url,
            headers=_remote_headers(server_url=base_url),
            timeout=timeout_s,
        ) as client:
            resp = client.request(method, path, params=params, json=json_body)
    except (httpx.HTTPError, OSError) as exc:
        return _HostHttpResult(
            status_code=0,
            body=f"{type(exc).__name__}: {exc}",
        )
    body: _HostJsonObject | str
    try:
        decoded = resp.json()
    except ValueError:
        body = resp.text
    else:
        body = cast(_HostJsonObject, decoded) if isinstance(decoded, dict) else str(decoded)
    return _HostHttpResult(status_code=resp.status_code, body=body)


def _host_error_text(body: _HostJsonObject | str) -> str:
    """
    Extract a concise error string from an Omnigent response body.

    :param body: Response body decoded by :func:`_host_http_json`.
    :returns: Human-readable error text.
    """
    if isinstance(body, str):
        return body[:400]
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return json.dumps(body)[:400]


def _daemon_session_request_params(
    *,
    connected_only: bool,
    after: str | None,
) -> dict[str, str | int]:
    """
    Build query parameters for one sessions page.

    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :param after: Optional cursor from the prior page, e.g.
        ``"conv_abc123"``.
    :returns: Query parameters for ``GET /v1/sessions``.
    """
    params: dict[str, str | int] = {
        "limit": 1000,
        "include_archived": "true",
    }
    if connected_only:
        params["connected"] = "true"
    if after is not None:
        params["after"] = after
    return params


def _decode_sessions_page(
    result: _HostHttpResult,
) -> _SessionsPageResult:
    """
    Decode one ``GET /v1/sessions`` response page.

    :param result: HTTP result returned by :func:`_host_http_json`.
    :returns: Decoded page result. ``error`` is ``None`` on success.
    """
    if result.status_code == 0:
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error=f"session list failed: {_host_error_text(result.body)}",
        )
    if result.status_code >= 400:
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error=(f"session list failed ({result.status_code}): {_host_error_text(result.body)}"),
        )
    if not isinstance(result.body, dict):
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error="session list returned a non-object response",
        )
    data = result.body.get("data")
    if not isinstance(data, list):
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error="session list returned a malformed data field",
        )
    rows = [s for s in data if isinstance(s, dict)]
    last_id = result.body.get("last_id")
    has_more = result.body.get("has_more")
    return _SessionsPageResult(
        sessions=rows,
        last_id=last_id if isinstance(last_id, str) and last_id else None,
        has_more=has_more if isinstance(has_more, bool) else False,
        error=None,
    )


def _fetch_session_pages(
    *,
    base_url: str,
    connected_only: bool,
) -> _SessionPagesResult:
    """
    Fetch every available session page from a server.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :returns: Accumulated sessions result. ``error`` is ``None`` on success.
    """
    after: str | None = None
    sessions: list[_HostSessionRow] = []
    while True:
        page_result = _host_http_json(
            base_url=base_url,
            method="GET",
            path="/v1/sessions",
            params=_daemon_session_request_params(
                connected_only=connected_only,
                after=after,
            ),
        )
        page = _decode_sessions_page(page_result)
        if page.error is not None:
            return _SessionPagesResult(sessions=[], error=page.error)
        sessions.extend(page.sessions)
        if not page.has_more or page.last_id is None:
            return _SessionPagesResult(sessions=sessions, error=None)
        after = page.last_id


def _sessions_for_daemon(
    record: _HostDaemonRecord,
    *,
    connected_only: bool = False,
) -> _DaemonSessionsResult:
    """
    Fetch sessions owned by a daemon's host id.

    :param record: Daemon record whose sessions should be listed.
    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :returns: Sessions result. ``error`` is ``None`` on success.
    """
    base_url = _daemon_base_url(record)
    if base_url is None:
        return _DaemonSessionsResult(
            base_url=None,
            sessions=[],
            error="local Omnigent server is not reachable",
        )
    host_id = record.host_id or _load_existing_host_id()
    if not host_id:
        return _DaemonSessionsResult(
            base_url=base_url,
            sessions=[],
            error="host id is not available in local config",
        )
    pages = _fetch_session_pages(
        base_url=base_url,
        connected_only=connected_only,
    )
    if pages.error is not None:
        return _DaemonSessionsResult(base_url=base_url, sessions=[], error=pages.error)
    owned = [s for s in pages.sessions if s.get("host_id") == host_id]
    return _DaemonSessionsResult(base_url=base_url, sessions=owned, error=None)


def _runner_online_map(
    *,
    base_url: str,
    sessions: list[_HostSessionRow],
) -> dict[str, bool | None]:
    """
    Resolve live runner connectivity for sessions.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param sessions: Session rows containing ``runner_id`` values.
    :returns: Map of ``runner_id`` to ``True`` / ``False``. ``None``
        means the runner status could not be resolved.
    """
    from omnigent.claude_native_bridge import url_component

    runner_ids = sorted(
        {
            runner_id
            for session in sessions
            if isinstance((runner_id := session.get("runner_id")), str) and runner_id
        }
    )
    statuses: dict[str, bool | None] = {}
    for runner_id in runner_ids:
        result = _host_http_json(
            base_url=base_url,
            method="GET",
            path=f"/v1/runners/{url_component(runner_id)}/status",
        )
        if result.status_code == 200 and isinstance(result.body, dict):
            online = result.body.get("online")
            statuses[runner_id] = online if isinstance(online, bool) else None
        else:
            statuses[runner_id] = None
    return statuses


def _annotate_sessions_with_runner_online(
    *,
    base_url: str,
    sessions: list[_HostSessionRow],
) -> list[_HostSessionRow]:
    """
    Add ``runner_online`` to session rows.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param sessions: Session rows returned by ``GET /v1/sessions``.
    :returns: Copies of the session rows with ``runner_online`` added.
    """
    statuses = _runner_online_map(base_url=base_url, sessions=sessions)
    annotated: list[_HostSessionRow] = []
    for session in sessions:
        runner_id = session.get("runner_id")
        runner_online = statuses.get(runner_id) if isinstance(runner_id, str) else None
        annotated.append({**session, "runner_online": runner_online})
    return annotated


def _base_daemon_status_payload(record: _HostDaemonRecord) -> _HostPayload:
    """
    Build daemon metadata for status output.

    :param record: Daemon registry record to inspect.
    :returns: JSON-serializable daemon metadata.
    """
    base_url = _daemon_base_url(record)
    host_id = record.host_id or _load_existing_host_id()
    return {
        "target": record.target,
        "mode": record.mode,
        "server_url": base_url,
        "pid": record.pid,
        "process": "online" if _pid_alive(record.pid) else "offline",
        "log_path": record.log_path,
        "host_id": host_id,
        "host_status": None,
        "sessions": [],
        "error": None,
    }


def _add_daemon_host_status(
    payload: _HostPayload,
) -> None:
    """
    Add host status or host status error to a daemon payload.

    :param payload: Payload from :func:`_base_daemon_status_payload`.
    """
    base_url = payload.get("server_url")
    host_id = payload.get("host_id")
    if not isinstance(base_url, str):
        payload["error"] = "local Omnigent server is not reachable"
        return
    if not isinstance(host_id, str) or not host_id:
        payload["error"] = "host id is not available in local config"
        return
    from omnigent.claude_native_bridge import url_component

    host_result = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/hosts/{url_component(host_id)}",
    )
    if host_result.status_code == 200 and isinstance(host_result.body, dict):
        status = host_result.body.get("status")
        payload["host_status"] = status if isinstance(status, str) else None
    elif host_result.status_code == 0:
        payload["error"] = f"host status failed: {_host_error_text(host_result.body)}"
    elif host_result.status_code >= 400:
        payload["error"] = (
            f"host status failed ({host_result.status_code}): {_host_error_text(host_result.body)}"
        )


def _add_daemon_sessions(
    payload: _HostPayload,
    record: _HostDaemonRecord,
    *,
    connected_sessions_only: bool,
) -> None:
    """
    Add owned sessions and runner connectivity to a daemon payload.

    :param payload: Payload from :func:`_base_daemon_status_payload`.
    :param record: Daemon registry record to inspect.
    :param connected_sessions_only: Whether session listing should use
        the server's connected filter.
    """
    sessions_result = _sessions_for_daemon(
        record,
        connected_only=connected_sessions_only,
    )
    sessions = sessions_result.sessions
    if sessions_result.base_url is not None and sessions:
        sessions = _annotate_sessions_with_runner_online(
            base_url=sessions_result.base_url,
            sessions=sessions,
        )
    payload["sessions"] = cast(_HostJsonValue, sessions)
    if sessions_result.error is not None and payload["error"] is None:
        payload["error"] = sessions_result.error


def _daemon_status_payload(
    record: _HostDaemonRecord,
    *,
    include_sessions: bool,
    connected_sessions_only: bool,
) -> _HostPayload:
    """
    Build a display payload for one daemon.

    :param record: Daemon registry record to inspect.
    :param include_sessions: Whether to include session rows.
    :param connected_sessions_only: Whether session listing should use
        the server's connected filter.
    :returns: JSON-serializable status payload.
    """
    payload = _base_daemon_status_payload(record)
    _add_daemon_host_status(payload)
    if include_sessions:
        _add_daemon_sessions(
            payload,
            record,
            connected_sessions_only=connected_sessions_only,
        )
    return payload


def _host_console() -> Console:
    """
    Build the Rich console used by host management output.

    :returns: A :class:`rich.console.Console` configured for predictable
        CLI rendering.
    """
    return Console(highlight=False)


def _host_table(title: str) -> Table:
    """
    Build a host CLI table with the shared style.

    :param title: Table title, e.g. ``"Host daemons"``.
    :returns: A :class:`rich.table.Table` ready for columns and rows.
    """
    return Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        border_style="dim",
        header_style="bold cyan",
        show_edge=False,
    )


def _host_display_value(value: _HostJsonValue, *, missing: str = "-") -> str:
    """
    Convert optional payload values into display text.

    :param value: Payload value, e.g. ``None`` or ``"runner_abc"``.
    :param missing: Text to use when *value* is absent, e.g. ``"-"``.
    :returns: Display string.
    """
    if value is None:
        return missing
    text = str(value)
    return text if text else missing


def _host_shorten(text: _HostJsonValue, *, max_chars: int) -> str:
    """
    Shorten long daemon, session, and runner identifiers for terminal display.

    :param text: Value to shorten, e.g. ``"conv_abcdef123456"``.
    :param max_chars: Maximum display width, e.g. ``24``.
    :returns: The original text if it fits, otherwise a middle-truncated
        string.
    """
    value = _host_display_value(text)
    if len(value) <= max_chars:
        return value
    if max_chars <= 1:
        return value[:max_chars]
    head = max(1, (max_chars - 1) // 2)
    tail = max(1, max_chars - head - 1)
    return f"{value[:head]}…{value[-tail:]}"


def _host_truncate(text: _HostJsonValue, *, max_chars: int) -> str:
    """
    Truncate long text from the right for compact terminal display.

    :param text: Value to truncate, e.g. an Omnigent error message.
    :param max_chars: Maximum display width, e.g. ``96``.
    :returns: The original text if it fits, otherwise a right-truncated
        string ending in an ellipsis.
    """
    value = _host_display_value(text)
    if len(value) <= max_chars:
        return value
    if max_chars <= 1:
        return value[:max_chars]
    return f"{value[: max_chars - 1]}…"


def _host_markup(text: _HostJsonValue, *, missing: str = "-") -> str:
    """
    Escape dynamic values before embedding them in Rich markup.

    :param text: Value to render, e.g. a session title containing ``"["``.
    :param missing: Text to use when *text* is absent, e.g. ``"-"``.
    :returns: Markup-safe display text.
    """
    from rich.markup import escape

    return escape(_host_display_value(text, missing=missing))


def _host_target_label(payload: _HostPayload, *, width: int) -> str:
    """
    Build a compact daemon target label.

    :param payload: Payload from :func:`_daemon_status_payload`.
    :param width: Maximum label width, e.g. ``48``.
    :returns: Compact target label for headers and error rows.
    """
    target = _host_display_value(payload.get("target"))
    server_url = payload.get("server_url")
    if target == _LOCAL_DAEMON_MARKER and server_url:
        target = f"local ({server_url})"
    return _host_shorten(target, max_chars=width)


def _host_status_style(value: _HostJsonValue) -> str:
    """
    Pick a Rich style for a daemon, host, or session status.

    :param value: Status value, e.g. ``"online"``, ``"idle"``, or
        ``"failed"``.
    :returns: Rich style name for the value.
    """
    status = _host_display_value(value).lower()
    if status in {"online", "connected", "running", "idle"}:
        return "green"
    if status in {"offline", "failed", "error", "unknown"}:
        return "red"
    return "yellow"


def _host_runner_state(session: _HostSessionRow) -> str:
    """
    Return a display state for the session's bound runner.

    :param session: Session row, e.g.
        ``{"runner_id": "runner_abc", "runner_online": True}``.
    :returns: ``"online"``, ``"offline"``, or ``"unknown"``.
    """
    runner_id = session.get("runner_id")
    if not isinstance(runner_id, str) or not runner_id:
        return "unknown"
    runner_online = session.get("runner_online")
    if runner_online is True:
        return "online"
    if runner_online is False:
        return "offline"
    return "unknown"


def _host_sessions_table_widths(
    *, console_width: int, sessions: list[_HostJsonValue]
) -> _HostSessionsTableWidths:
    """
    Compute compact sessions table widths for the available terminal space.

    :param console_width: Console width in cells, e.g. ``120``.
    :param sessions: Raw session payloads from status data.
    :returns: Column widths that prefer full IDs when they fit.
    """
    rows = [session for session in sessions if isinstance(session, dict)]
    full_session_id = max(
        [len("Session ID"), *[len(_host_display_value(row.get("id"))) for row in rows]]
    )
    full_runner_id = max(
        [len("Runner ID"), *[len(_host_display_value(row.get("runner_id"))) for row in rows]]
    )
    min_title = 12
    # Padding, separators, and the fixed State / Runner columns consume
    # space that is not represented by the three variable-width columns.
    table_chrome = 34
    full_ids_fit = console_width >= full_session_id + full_runner_id + min_title + table_chrome
    session_id = full_session_id if full_ids_fit else min(full_session_id, 18)
    runner_id = full_runner_id if full_ids_fit else min(full_runner_id, 20)
    title = max(min_title, min(console_width - session_id - runner_id - table_chrome, 60))
    workspace = 48 if console_width >= session_id + runner_id + title + table_chrome + 50 else None
    return _HostSessionsTableWidths(
        session_id=session_id,
        runner_id=runner_id,
        title=title,
        workspace=workspace,
    )


def _add_host_payload_sessions_table(console: Console, payload: _HostPayload) -> None:
    """
    Render one daemon's owned sessions as a compact table.

    :param console: Rich console returned by :func:`_host_console`.
    :param payload: Payload from :func:`_daemon_status_payload`.
    """
    raw_sessions = payload.get("sessions")
    sessions = raw_sessions if isinstance(raw_sessions, list) else []
    if not sessions:
        console.print("  [dim]No owned sessions found.[/dim]")
        return
    table = _host_table("Sessions")
    widths = _host_sessions_table_widths(console_width=console.width, sessions=sessions)
    table.add_column(
        "Session ID",
        style="bold",
        overflow="ellipsis",
        no_wrap=True,
        max_width=widths.session_id,
    )
    table.add_column("State", width=7, no_wrap=True)
    table.add_column("Runner", width=7, no_wrap=True)
    table.add_column(
        "Runner ID",
        overflow="ellipsis",
        no_wrap=True,
        max_width=widths.runner_id,
    )
    table.add_column(
        "Title",
        overflow="ellipsis",
        no_wrap=True,
        max_width=widths.title,
    )
    if widths.workspace is not None:
        table.add_column(
            "Workspace",
            overflow="ellipsis",
            no_wrap=True,
            max_width=widths.workspace,
        )
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_row = session
        status = _host_display_value(session_row.get("status"), missing="unknown")
        runner_state = _host_runner_state(session_row)
        row = [
            _host_shorten(session_row.get("id"), max_chars=widths.session_id),
            f"[{_host_status_style(status)}]{status}[/]",
            f"[{_host_status_style(runner_state)}]{runner_state}[/]",
            _host_shorten(session_row.get("runner_id"), max_chars=widths.runner_id),
            _host_truncate(
                session_row.get("title"),
                max_chars=widths.title,
            ),
        ]
        if widths.workspace is not None:
            row.append(_host_shorten(session_row.get("workspace"), max_chars=widths.workspace))
        table.add_row(*row)
    console.print(table)


def _echo_daemon_payloads(payloads: list[_HostPayload]) -> None:
    """
    Render host status as one block per daemon target.

    :param payloads: Payloads from :func:`_daemon_status_payload`.
    """
    console = _host_console()
    if not payloads:
        console.print("[dim]No host daemons found.[/dim]")
        return
    for idx, payload in enumerate(payloads):
        if idx:
            console.print()
        target = _host_target_label(payload, width=max(24, min(console.width - 2, 96)))
        process = _host_display_value(payload.get("process"), missing="unknown")
        host_status = _host_display_value(payload.get("host_status"), missing="unknown")
        console.print(f"[bold cyan]{_host_markup(target)}[/bold cyan]")
        console.print(
            "  "
            f"mode={_host_markup(payload.get('mode'))}  "
            f"pid={_host_markup(payload.get('pid'))}  "
            f"process=[{_host_status_style(process)}]{process}[/]  "
            f"host=[{_host_status_style(host_status)}]{host_status}[/]"
        )
        server_text = _host_shorten(
            payload.get("server_url"),
            max_chars=max(24, console.width - 11),
        )
        console.print(f"  server={_host_markup(server_text)}")
        console.print(f"  host_id={_host_markup(payload.get('host_id'))}")
        if payload.get("log_path"):
            console.print(f"  log={_host_markup(payload.get('log_path'))}")
        if payload.get("error"):
            message = _host_truncate(
                payload.get("error"),
                max_chars=max(24, console.width - 10),
            )
            console.print(f"  [red]error={_host_markup(message)}[/red]")
        _add_host_payload_sessions_table(console, payload)


@host.command("status")
@click.option("--server", default=None, help="Inspect only this server target.")
@click.option("--all", "all_targets", is_flag=True, help="Inspect all known daemon targets.")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@click.pass_context
def host_status(
    ctx: click.Context,
    server: str | None,
    all_targets: bool,
    json_output: bool,
) -> None:
    """
    Inspect host daemon, runner, and session status.

    :param ctx: Click context carrying group-level options.
    :param server: Optional server target to inspect, e.g.
        ``"https://example.databricksapps.com"``.
    :param all_targets: Whether to inspect every known daemon target.
    :param json_output: Whether to emit machine-readable JSON.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    records = _selected_daemon_records(server=server, all_targets=all_targets, default_all=True)
    payloads = [
        _daemon_status_payload(
            record,
            include_sessions=True,
            connected_sessions_only=True,
        )
        for record in records
    ]
    if json_output:
        click.echo(json.dumps({"daemons": payloads}, indent=2, sort_keys=True))
        return
    _echo_daemon_payloads(payloads)


def _stop_session_on_server(
    *,
    base_url: str,
    session_id: str,
) -> None:
    """
    Stop one Omnigent session via the server lifecycle event API.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :raises click.ClickException: If the server rejects the stop event.
    """
    from omnigent.claude_native_bridge import url_component

    result = _host_http_json(
        base_url=base_url,
        method="POST",
        path=f"/v1/sessions/{url_component(session_id)}/events",
        json_body={"type": "stop_session", "data": {}},
    )
    if result.status_code == 0:
        raise click.ClickException(
            f"Failed to stop session {session_id!r}: {_host_error_text(result.body)}"
        )
    if result.status_code >= 400:
        raise click.ClickException(
            f"Failed to stop session {session_id!r} ({result.status_code}): "
            f"{_host_error_text(result.body)}"
        )


def _stop_daemon_sessions(
    record: _HostDaemonRecord,
    *,
    force: bool,
) -> int:
    """
    Stop sessions owned by a daemon before terminating it.

    :param record: Daemon record whose host-bound sessions should stop.
    :param force: Continue stopping remaining sessions after failures.
    :returns: Number of sessions successfully stopped.
    :raises click.ClickException: If session listing or stop fails and
        ``force`` is ``False``.
    """
    result = _sessions_for_daemon(record)
    if result.error is not None:
        if force:
            click.echo(f"{record.target}: skipping session stop: {result.error}", err=True)
            return 0
        raise click.ClickException(f"{record.target}: {result.error}")
    if result.base_url is None:
        return 0
    stopped = 0
    for session in result.sessions:
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        try:
            _stop_session_on_server(
                base_url=result.base_url,
                session_id=session_id,
            )
        except click.ClickException as exc:
            if not force:
                raise
            click.echo(str(exc), err=True)
            continue
        stopped += 1
    return stopped


def _terminate_daemon(record: _HostDaemonRecord, *, force: bool) -> None:
    """
    Terminate one local daemon process.

    :param record: Daemon record whose process should terminate.
    :param force: Send SIGKILL after the SIGTERM grace period.
    :raises click.ClickException: If the process stays alive.
    """
    if not _pid_alive(record.pid):
        _delete_daemon_record(record)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + _HOST_DAEMON_STOP_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(record.pid):
            _delete_daemon_record(record)
            return
        time.sleep(0.1)
    if force:
        with contextlib.suppress(ProcessLookupError):
            os.kill(record.pid, signal.SIGKILL)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not _pid_alive(record.pid):
                _delete_daemon_record(record)
                return
            time.sleep(0.1)
    raise click.ClickException(
        f"Daemon {record.pid} for {record.target!r} did not exit; retry with --force."
    )


@host.command("stop")
@click.option("--server", default=None, help="Stop only this server target.")
@click.option("--all", "all_targets", is_flag=True, help="Stop all known daemon targets.")
@click.option(
    "--daemon-only",
    is_flag=True,
    help="Terminate daemon processes without first stopping sessions.",
)
@click.option("--force", is_flag=True, help="Continue after failures and use SIGKILL if needed.")
@click.pass_context
def host_stop(
    ctx: click.Context,
    server: str | None,
    all_targets: bool,
    daemon_only: bool,
    force: bool,
) -> None:
    """
    Stop host daemon sessions, then stop daemon processes.

    :param ctx: Click context carrying group-level options.
    :param server: Optional server target to stop, e.g.
        ``"https://example.databricksapps.com"``.
    :param all_targets: Whether to stop every known daemon target.
    :param daemon_only: Skip server-side session stop calls when ``True``.
    :param force: Continue after failures and use SIGKILL if needed.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    records = _selected_daemon_records(server=server, all_targets=all_targets, default_all=False)
    if not records:
        click.echo("No matching host daemon found.")
        return
    for record in records:
        stopped = 0
        if not daemon_only:
            stopped = _stop_daemon_sessions(record, force=force)
        _terminate_daemon(record, force=force)
        click.echo(f"Stopped {record.target} daemon pid={record.pid}; sessions_stopped={stopped}.")


@host.command("stop-session")
@click.argument("session_ids", nargs=-1, required=True)
@click.option("--server", default=None, help="Server that owns the sessions.")
@click.option("--force", is_flag=True, help="Continue after individual stop failures.")
@click.pass_context
def host_stop_session(
    ctx: click.Context,
    session_ids: Sequence[str],
    server: str | None,
    force: bool,
) -> None:
    """
    Stop specific sessions without stopping a daemon.

    :param ctx: Click context carrying group-level options.
    :param session_ids: Session ids to stop, e.g.
        ``["conv_abc123", "conv_def456"]``.
    :param server: Omnigent server URL that owns the sessions, e.g.
        ``"https://example.databricksapps.com"``. ``None`` falls back
        to config/local discovery.
    :param force: Continue after individual stop failures.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    resolved_server = _resolve_host_server(server)
    if resolved_server is None:
        resolved_server = local_server_url_if_healthy()
        if resolved_server is None:
            raise click.ClickException(
                "No server was supplied and no local Omnigent server is reachable."
            )
    for session_id in session_ids:
        try:
            _stop_session_on_server(
                base_url=resolved_server,
                session_id=session_id,
            )
        except click.ClickException:
            if not force:
                raise
            click.echo(f"Failed to stop session {session_id!r}.", err=True)
            continue
        click.echo(f"Stopped session {session_id}.")


