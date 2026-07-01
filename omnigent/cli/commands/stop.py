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

@cli.command("stop")
@click.option(
    "--force",
    is_flag=True,
    help="Continue past failures and SIGKILL daemons that do not exit on SIGTERM.",
)
def stop(force: bool) -> None:
    """Stop everything Omnigent is running on this machine.

    The off switch: stops every host daemon (local and remote-targeted)
    and the detached background server. Runners are reaped when their daemon
    exits. To stop only hosting while keeping the local server (web UI /
    history) up, use ``omnigent host stop`` instead.

    :param force: Continue past individual failures and SIGKILL daemons that
        do not exit on SIGTERM.
    :returns: None.
    """
    stopped = 0
    failures: list[str] = []
    for record in _list_daemon_records():
        # Terminating the daemon reaps its runners (orphan-watchdog), so the
        # off-switch doesn't need the graceful per-session HTTP stop that
        # `host stop` does — that keeps teardown quiet and dependency-free.
        try:
            _terminate_daemon(record, force=force)
            stopped += 1
        except click.ClickException as exc:
            failures.append(exc.message)
    server_was_running = local_server_url_if_healthy() is not None
    stop_local_omnigent_server()
    # Sweep the canonical port for an orphaned server the pidfile lost track
    # of (a torn/cleared record, or a respawn that landed elsewhere). Without
    # this, that server survives the off-switch — the exact "I ran stop and a
    # server is still on the default port" symptom.
    orphan_pid = stop_untracked_local_server()

    parts: list[str] = []
    if stopped:
        parts.append(f"{stopped} daemon(s)")
    if server_was_running:
        parts.append("the background server")
    if orphan_pid is not None:
        parts.append(f"an untracked server on :{_DEFAULT_LOCAL_PORT} (pid {orphan_pid})")
    if parts:
        click.echo("Stopped " + " and ".join(parts) + ".")
    else:
        click.echo("Nothing to stop.")
    if failures:
        raise click.ClickException("; ".join(failures) + " — retry with --force.")


def _count_running_sessions(base_url: str) -> int:
    """Count sessions actively running a turn on the local server.

    Gates on the session-list ``status`` field (``"running"`` — a runner
    mid-turn, or with a still-running sub-agent), NOT mere connectedness:
    an idle session keeps its host/runner connection open indefinitely, so
    counting connected sessions would make the drain wait forever for
    sessions that aren't doing any work. Only ``"running"`` sessions hold
    in-flight work an upgrade should avoid interrupting.

    A transient HTTP failure is treated as "none running" rather than
    blocking the upgrade — the server's own graceful shutdown still drains
    any runner that happens to be mid-turn.

    :param base_url: Local server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :returns: Number of sessions with ``status == "running"``, or ``0`` on
        a query failure.
    """
    with contextlib.suppress(click.ClickException):
        pages = _fetch_session_pages(base_url=base_url, connected_only=True)
        return sum(1 for session in pages.sessions if session.get("status") == "running")
    return 0


def _wait_for_local_sessions_to_drain() -> None:
    """Block until no local session is actively running a turn.

    Used by ``omni upgrade`` (without ``--force``) so an upgrade never
    yanks a running agent turn. Waits only on sessions whose status is
    ``"running"`` (see :func:`_count_running_sessions`) — idle-but-connected
    sessions do not hold it up. Polls every :data:`_UPGRADE_DRAIN_POLL_S`
    seconds and re-prints the count whenever it changes; ``Ctrl-C`` aborts
    the wait (and the upgrade) cleanly. Returns immediately when the server
    is down or already idle.
    """
    info = local_server_status()
    if not (info.running and info.url is not None):
        return
    count = _count_running_sessions(info.url)
    if count == 0:
        return
    click.echo(
        f"Waiting for {count} running session(s) to finish — press Ctrl-C to "
        "abort, or re-run with --force to stop them now."
    )
    last = count
    while True:
        time.sleep(_UPGRADE_DRAIN_POLL_S)
        info = local_server_status()
        if not (info.running and info.url is not None):
            return
        count = _count_running_sessions(info.url)
        if count == 0:
            return
        if count != last:
            click.echo(f"  {count} session(s) still running…")
            last = count


