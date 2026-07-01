"""CLI entry point for omnigent."""

from __future__ import annotations

import collections.abc
import contextlib
import copy
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeAlias, cast

import click
import yaml
from pydantic import BaseModel, ConfigDict
from rich import box
from rich.console import Console
from rich.table import Table

from omnigent._startup_profile import StartupProfiler
from omnigent.cli_sandbox import lakebox as _lakebox_alias_group
from omnigent.cli_sandbox import sandbox as _sandbox_group
from omnigent.harness_aliases import canonicalize_harness
from omnigent.host.local_server import (
    _DEFAULT_LOCAL_PORT,
    _pid_alive,
    ensure_local_omnigent_server,
    local_server_status,
    local_server_url_if_healthy,
    server_config_signature,
    stop_local_omnigent_server,
    stop_untracked_local_server,
)
from omnigent.onboarding.sandboxes import available_providers as _sandbox_providers
from omnigent.onboarding.ucode_setup import (
    build_ucode_configure_command,
    find_ucode_command,
    model_gateway_workspace_urls,
)

if TYPE_CHECKING:
    import httpx

    from omnigent._runner_startup import RunnerStartupProgress
    from omnigent.onboarding.ambient import DetectedProvider
    from omnigent.onboarding.provider_config import ProviderEntry


# Any: YAML configs have heterogeneous value types (str, int, list, etc.)
def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _default_db_uri() -> str:
    """Default DB URI for ``omnigent server`` — the machine-global
    ``<data_dir>/chat.db``.

    Resolves to the same path the ``omnigent run`` daemon spawns its
    local server against (``_local_data_dir()``, honoring
    ``OMNIGENT_DATA_DIR`` → else ``~/.omnigent``). Pinning ``server``
    to the same DB as ``run`` means there is **one local DB — and so one
    accounts admin — per machine**, instead of a fresh CWD-relative
    ``omnigent.db`` (and a fresh admin) for every directory you launch
    from. ``--database-uri`` / the config file still override.

    :returns: e.g. ``"sqlite:////home/alice/.omnigent/chat.db"``.
    """
    from omnigent.host.local_server import _local_data_dir

    return f"sqlite:///{_local_data_dir() / 'chat.db'}"

def _maybe_prompt_first_admin(account_store: Any, auth_provider: Any, *, auto_open: bool) -> None:  # type: ignore[explicit-any]  # SqlAlchemyAccountStore | None, AuthProvider
    """Interactively claim the first admin on a TTY when setup is pending.

    The "terminal" entry point of first-run setup. It's the FALLBACK,
    not the default: when the browser is about to auto-open the web
    Create-admin form (the default ``--open`` on a loopback server), we
    skip the prompt and let the browser own setup — otherwise the
    terminal prompt would block before the lifespan ever opens the
    browser, so the form would never appear.

    No-ops unless ALL of:

    - accounts mode is active (``account_store`` is not ``None``);
    - no password-having account exists yet (a ``--admin-password`` /
      ``INIT_ADMIN_PASSWORD`` would already have created one, and a
      re-boot already has an admin);
    - stdin AND stdout are a TTY — a headless / piped / agent run must
      NOT block on a prompt (it falls through to the web form);
    - the browser is NOT auto-opening a usable form, i.e. ``--no-open``
      was passed OR the base URL isn't loopback (remote-over-SSH, where
      opening a browser on the server box is useless but a terminal IS
      available).

    On success, creates the admin and mints the loopback CLI token so a
    subsequent ``omnigent run`` against this server is signed in.

    :param account_store: The accounts store, or ``None`` in
        header/OIDC mode (then this is a no-op).
    :param auth_provider: The active auth provider; its accounts config
        supplies the cookie secret / base URL / session TTL.
    :param auto_open: The resolved ``--open/--no-open`` flag. When True
        and the base URL is loopback, the lifespan opens the browser to
        the form, so we defer to it and skip the prompt.
    :returns: None.
    """
    if account_store is None:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    if any(u.has_password for u in account_store.list_users()):
        return

    from omnigent.server.accounts_bootstrap import (
        _is_loopback_base_url,
        _mint_loopback_cli_token,
        resolve_admin_username,
    )
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.passwords import hash_password
    from omnigent.server.routes.accounts_auth import _MIN_PASSWORD_LENGTH

    # Read the accounts config off the concrete provider (same direct
    # access app.py uses). isinstance-narrowed so mypy sees the attribute
    # rather than reaching through getattr(..., "<literal>").
    base_url: str | None = None
    if isinstance(auth_provider, UnifiedAuthProvider):
        cfg = auth_provider._accounts_config
        base_url = cfg.base_url if cfg is not None else None
    # Defer to the browser form when it's going to open (default --open
    # on a loopback server). Only prompt when no browser form will appear.
    if auto_open and base_url is not None and _is_loopback_base_url(base_url):
        return

    click.echo("\n  First-run setup — create the admin account for this server.")
    username = click.prompt("  Username", default=resolve_admin_username()).strip().lower()
    while True:
        password = click.prompt("  Password", hide_input=True, confirmation_prompt=True)
        if len(password) >= _MIN_PASSWORD_LENGTH:
            break
        click.echo(f"  Password must be at least {_MIN_PASSWORD_LENGTH} characters.", err=True)

    try:
        account_store.create_user_with_password(username, hash_password(password), is_admin=True)
    except ValueError:
        # Raced another claimer (e.g. someone hit the web form first).
        click.echo("  An admin was just created elsewhere — skipping.", err=True)
        return

    # Mint the loopback CLI token so `omnigent run` is signed in.
    # (Reuses cfg/base_url resolved above.)
    if (
        cfg is not None
        and base_url is not None
        and cfg.cookie_secret is not None
        and _is_loopback_base_url(base_url)
    ):
        _mint_loopback_cli_token(
            username,
            base_url=base_url,
            cookie_secret=cfg.cookie_secret,
            session_ttl_hours=cfg.session_ttl_hours,
        )
    click.echo(f"  ✓ Admin '{username}' created. Sign in at the server URL.\n")

def _create_artifact_store(location: str) -> Any:  # type: ignore[explicit-any]  # returns ArtifactStore protocol (optional deps)
    """
    Create an artifact store based on the location URI scheme.

    Delegates to the shared store factory so the CLI, server bootstrapper, and
    container entrypoint use the same pluggable artifact-store selection path.

    :param location: Artifact storage location, e.g.
        ``"./artifacts"`` for local,
        ``"dbfs:/Volumes/cat/schema/vol"`` for UC Volumes, or
        ``"nats://host:4222/omnigent-artifacts"`` for JetStream.
    :returns: An :class:`ArtifactStore` instance.
    """
    from omnigent.stores.factory import _create_artifact_store as _shared_factory

    return _shared_factory(location)

def _preregister_agent(  # type: ignore[explicit-any]  # agent_store / artifact_store / agent_cache typed Any to avoid import cycle
    agent_source: Path,
    agent_store: Any,
    artifact_store: Any,
    agent_cache: Any,
) -> str | None:
    """
    Register an agent from a directory or standalone YAML file.

    Materializes *agent_source* into a uniform bundle directory via
    :func:`omnigent.spec.materialize_bundle`, tars it, validates
    the spec, and creates (or replaces) the agent in the store. This
    runs at server startup for each ``--agent`` flag.

    :param agent_source: Either an agent-image directory containing
        ``config.yaml`` (standard omnigent shape) or a standalone
        omnigent YAML file (e.g.
        ``examples/coding_supervisor.yaml``). The file-vs-directory
        branch lives inside ``materialize_bundle``; this function
        operates uniformly on a directory downstream of it.
    :param agent_store: The AgentStore for agent metadata.
    :param artifact_store: The ArtifactStore for bundle storage.
    :param agent_cache: The AgentCache. Required so the on-disk
        extracted-bundle tier (cache_dir/<agent_id>/) is swapped
        in lockstep with the artifact-store update — otherwise a
        persistent session reuses the prior extraction and any
        newly-added local-tool files (or other bundle edits) are
        silently ignored on the next request.
    :returns: The registered agent id, or ``None`` if the source
        spec has no name and is skipped.
    """
    import gzip
    import hashlib
    import io
    import tarfile

    from omnigent.db.utils import generate_agent_id
    from omnigent.spec import load, materialize_bundle

    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(agent_source, Path(tmpdir) / "bundle")

        # Build tarball in memory from the materialized bundle dir.
        # ``arcname="."`` puts the contents at the tarball root so
        # extraction produces the same shape ``spec.load`` expects.
        # Pin gzip mtime so sha256(bundle_bytes) is deterministic across calls.
        buf = io.BytesIO()
        with (
            gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w") as tar,
        ):
            tar.add(str(bundle_dir), arcname=".")
        bundle_bytes = buf.getvalue()

        # Validate via the materialized directory directly — cheaper
        # than round-tripping through extract.
        spec = load(bundle_dir)

    if spec.name is None:
        click.echo(f"  warning: {agent_source} has no name, skipping")
        return None

    # Idempotent registration. Mirrors
    # :func:`omnigent.inner.cli._omnigent_register_yaml_bundle` —
    # see designs/RUN_OMNIGENT_SESSION_RESUMPTION.md. Reusing the
    # existing ``agent_id`` (rather than delete + recreate)
    # is load-bearing for ``--continue``: deleting the old
    # row cascades through the ``tasks`` FK
    # (``ondelete=CASCADE`` in
    # :class:`omnigent.db.db_models.SqlTask`), wiping every
    # prior task — which makes the next ``--continue``
    # filter by ``agent_id`` return zero conversations and
    # exit ``"No prior conversation for agent ..."``. Update
    # the bundle in place and only refresh
    # ``bundle_location`` when the content hash actually
    # changed so the row stays stable across no-op restarts.
    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    existing = agent_store.get_by_name(spec.name)
    if existing is not None:
        new_loc = f"{existing.id}/{bundle_hash}"
        if existing.bundle_location != new_loc:
            artifact_store.put(new_loc, bundle_bytes)
            agent_store.update(existing.id, bundle_location=new_loc)
            # Swap the cache's extracted bundle in lockstep. Without
            # this, ``AgentCache.load`` will hit Tier 2 (disk —
            # ``cache_dir/<agent_id>/``) on the next request and
            # return the OLD spec, even though the artifact store
            # and the DB row both point at the new bundle.
            # Mirrors what the HTTP PUT /agents/{id} route does at
            # ``omnigent/server/routes/agents.py:248``.
            # ``--agent`` registers operator-authored template agents,
            # so ${VAR} may expand against the server env here.
            agent_cache.replace(existing.id, new_loc, bundle_bytes, expand_env=True)
        click.echo(f"  agent: {spec.name} (from {agent_source})")
        return cast(str, existing.id)

    agent_id = generate_agent_id()
    loc = f"{agent_id}/{bundle_hash}"
    artifact_store.put(loc, bundle_bytes)
    agent_store.create(
        agent_id=agent_id,
        name=spec.name,
        bundle_location=loc,
        description=spec.description,
    )
    click.echo(f"  agent: {spec.name} (from {agent_source})")
    return agent_id

def _is_server_url(value: str) -> bool:
    """Return whether *value* is a server URL.

    :param value: CLI argument value, e.g. ``"http://localhost:6767"``.
    :returns: ``True`` for ``http://`` or ``https://`` URLs.
    """
    return value.startswith(("http://", "https://"))

def _ensure_databricks_server_auth(server: str) -> None:
    """Sign in (or fail with the login hint) for Databricks-fronted servers.

    Probes ``/v1/me`` with whatever credentials the auth chain can mint
    today. A non-200 answer that carries the Databricks edge signature
    (302 to the workspace OAuth page, or a DatabricksRealm 401) means
    the run would otherwise die much later with an opaque "non-JSON
    response (status=302)" traceback from the session-create call. On a
    TTY we run the same flow ``omnigent login`` would and continue;
    headless invocations get the exact command to run instead.

    Non-Databricks postures are deliberately left alone: local accounts
    servers auto-authenticate downstream (magic-link redeem), and
    header-mode servers answer 200 outright.

    :param server: Remote server base URL without a trailing slash,
        e.g. ``"https://myapp-123.aws.databricksapps.com"``.
    :raises click.ClickException: When the server is Databricks-fronted,
        no credentials resolve, and stdin is not a TTY (or the login
        flow itself fails).
    """
    import httpx as _httpx

    from omnigent.chat import _remote_headers

    try:
        probe = _httpx.get(
            f"{server}/v1/me",
            headers=_remote_headers(server_url=server),
            timeout=10.0,
        )
    except _httpx.HTTPError:
        # Unreachable / transient: let the connect path raise its own,
        # already-actionable error rather than failing the pre-flight.
        return
    if probe.status_code == 200:
        return
    workspace_host = _databricks_workspace_login_target(server, probe)
    if workspace_host is None:
        return
    login_cmd = f"omnigent login {server}"
    if not sys.stdin.isatty():
        raise click.ClickException(
            f"Not signed in to {server} (Databricks-fronted; /v1/me answered "
            f"HTTP {probe.status_code}). Run `{login_cmd}` and retry."
        )
    click.echo(f"Not signed in to {server} — running `{login_cmd}` first.")
    _databricks_login(server, workspace_host)

def _ensure_backend(server: str | None) -> str:
    """Ensure the host daemon is running and return the Omnigent server URL.

    The daemon is the single backend for ``attach`` / ``run`` / ``claude`` /
    ``codex``: it spawns the runner and, in local mode, the Omnigent server too.
    The CLI is a pure client of the returned URL.

    :param server: ``--server`` value after config fallback. A non-empty
        value targets that (remote or explicit-local) server. ``None`` or
        ``""`` selects local mode: the daemon starts (or reuses) a
        persistent local Omnigent server and this returns its discovered loopback
        URL.
    :returns: A concrete base URL, e.g. ``"http://127.0.0.1:8123"`` or the
        remote URL passed in.
    :raises click.ClickException: If local mode's server never becomes
        reachable.
    """
    from omnigent._runner_startup import (
        STARTUP_PHASE_CONNECTING_REMOTE,
        STARTUP_PHASE_LOCAL_SERVER,
        STARTUP_PHASE_STARTING,
        runner_startup_progress,
    )

    if server:
        # Remote / explicit-server mode: the server isn't ours to restart, so
        # there's no auth-mode-flip "re-run" to surface (config_changed is
        # always False for a non-local target). Expand a bare workspace URL
        # to its /api/2.0/omnigent mount, then sign in first when the
        # server is Databricks-fronted and we hold no usable credentials —
        # otherwise the session-create call deep in the REPL bring-up
        # surfaces the edge redirect as an opaque non-JSON-response
        # traceback.
        server = _workspace_api_server_url(server)
        _ensure_databricks_server_auth(server)
        with runner_startup_progress(initial_message=STARTUP_PHASE_CONNECTING_REMOTE):
            _ensure_host_daemon(server)
        return server
    # Local mode: the daemon spawns (or reuses) a persistent local Omnigent server.
    # On a cold start this is the longest silent gap between the user pressing
    # Enter and any output, so render a spinner whose label tracks the step.
    # It clears on context exit — before any auth-mode-change echo below and
    # before the REPL/terminal the caller brings up — and falls back to plain
    # stderr lines off a TTY (CI, daemon logfiles).
    with runner_startup_progress(initial_message=STARTUP_PHASE_STARTING) as progress:
        config_changed = _ensure_host_daemon(None)
        progress.update(STARTUP_PHASE_LOCAL_SERVER)
        local_url = _discover_local_server_url()
    _update_daemon_resolved_server_url(_LOCAL_DAEMON_MARKER, local_url)
    if config_changed:
        _exit_for_auth_mode_change(local_url)
    return local_url

def _exit_for_auth_mode_change(base_url: str) -> None:
    """Tell the user the server was restarted in a new mode, then exit clean.

    The local Omnigent server bakes its auth posture (header vs accounts, cookie
    secret) at boot, so an ``OMNIGENT_AUTH_ENABLED`` flip restarts it
    via :func:`_ensure_host_daemon`. Continuing the *same* command across
    that restart is brittle — the in-flight session/credential/terminal
    bring-up straddles two server identities. Instead we stop here with a
    clear, actionable message and exit 0, so the next ``omnigent run`` is
    a clean single-mode start. When the new mode is accounts and no admin
    exists yet, point the user at the one-time setup URL.

    :param base_url: The freshly-restarted Omnigent server URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :returns: Never returns — raises ``SystemExit(0)``.
    :raises SystemExit: Always, with code 0 (a clean, expected stop).
    """
    needs_admin_setup = False
    result = _host_http_json(base_url=base_url, method="GET", path="/v1/info")
    if result.status_code == 200 and isinstance(result.body, dict):
        needs_admin_setup = bool(
            result.body.get("accounts_enabled") and result.body.get("needs_setup")
        )

    click.echo("", err=True)
    click.echo("  ✓ Auth mode changed — the local server was restarted to match.", err=True)
    if needs_admin_setup:
        click.echo(
            f"  Create your one-time admin account at  {base_url.rstrip('/')}  "
            "(it may have opened automatically),",
            err=True,
        )
        click.echo("  then re-run `omnigent run` to start.", err=True)
    else:
        click.echo("  Re-run `omnigent run` to start.", err=True)
    click.echo("", err=True)
    raise SystemExit(0)

def _discover_local_server_url(
    timeout: float = _LOCAL_SERVER_DISCOVER_TIMEOUT_S,
) -> str:
    """Poll until the daemon-started local Omnigent server is reachable.

    In local mode the daemon owns the Omnigent server; the CLI discovers its URL
    via the local-server pidfile + ``/health`` rather than starting it
    itself.

    :param timeout: Max seconds to wait, e.g. ``60.0``.
    :returns: The loopback server URL, e.g. ``"http://127.0.0.1:8123"``.
    :raises click.ClickException: If the daemon exits first, or the server
        does not come up within the timeout.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = local_server_url_if_healthy()
        if url is not None:
            return url
        if not _host_daemon_alive():
            raise click.ClickException(
                "The local daemon exited before its Omnigent server became ready. "
                "See logs under ~/.omnigent/logs/host-daemon/ and "
                "~/.omnigent/logs/server/."
            )
        time.sleep(0.2)
    raise click.ClickException(
        f"Timed out after {timeout:.0f}s waiting for the local Omnigent server to "
        "start. See ~/.omnigent/logs/server/ for details."
    )

def _assert_server_port_bindable(host: str, port: int) -> None:
    """
    Fail before app startup when the requested TCP listener cannot bind.

    Mirrors uvicorn's TCP bind shape closely enough for CLI preflight:
    IPv6 is selected when the host contains ``":"``, and
    ``SO_REUSEADDR`` is set before bind. This is intentionally a bind
    probe, not a connect probe, so a failed client connection to the
    port does not make us report the port as occupied.

    :param host: Interface to bind, e.g. ``"127.0.0.1"``.
    :param port: TCP port to bind, e.g. ``6767``.
    :returns: None.
    :raises click.ClickException: If the host/port cannot be bound.
    """
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family=family, type=socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            reason = exc.strerror or str(exc)
            raise click.ClickException(
                f"Cannot start server on {host}:{port}: port is unavailable ({reason})."
            ) from exc

