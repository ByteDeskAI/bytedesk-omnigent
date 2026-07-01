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


@cli.group("debug")
def debug() -> None:
    """Internal maintenance commands (advanced — not needed for normal use).

    Houses operator-only database and accounts maintenance: tracking-DB
    schema upgrades (``db-upgrade``) and the accounts→OIDC identity remap
    (``migrate-accounts-to-oidc``).
    """


@debug.command("db-upgrade")
@click.argument("url")
def debug_db_upgrade(url: str) -> None:
    """
    Upgrade the schema of an Omnigent tracking database to the
    latest supported version.

    URL is a SQLAlchemy database URL, e.g.
    ``sqlite:////absolute/path/to/chat.db`` or
    ``postgresql://user:pass@host/dbname``.

    \b
    IMPORTANT: schema migrations can be slow and are not guaranteed
    to be transactional — always take a backup of your database
    before running migrations.
    """
    from sqlalchemy import create_engine

    from omnigent.db.utils import _run_migrations

    click.echo(f"Upgrading {url} ...")
    engine = create_engine(url)
    try:
        _run_migrations(engine, url)
    finally:
        engine.dispose()
    click.echo("Upgrade complete.")


@debug.command("import-sql-agents")
@click.option(
    "--database-uri",
    default=None,
    help="SQLAlchemy database URI to import legacy agents from. Defaults to config/default.",
)
@click.option(
    "--artifact-location",
    default=None,
    help="Artifact location used to resolve the active NATS AgentStore.",
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="YAML config file supplying database_uri/artifact_location.",
)
def debug_import_sql_agents(
    database_uri: str | None,
    artifact_location: str | None,
    config_path: str | None,
) -> None:
    """Import legacy SQL agent rows into the active NATS AgentStore."""
    from omnigent.stores.agent_store.import_sql import import_sql_agents
    from omnigent.stores.factory import _create_agent_store

    cfg = _load_config(config_path)
    db_uri = database_uri or cfg.get("database_uri", _default_db_uri())
    art_loc = artifact_location or cfg.get("artifact_location", _default_artifact_location())

    if config_path and artifact_location is None and _is_relative_artifact_location(art_loc):
        art_loc = str(Path(config_path).parent / art_loc)

    _ensure_sqlite_parent_dir(db_uri)
    agent_store = _create_agent_store(art_loc)
    report = import_sql_agents(db_uri, agent_store)
    if report.conflicts:
        conflicts = ", ".join(report.conflicts)
        raise click.ClickException(
            "Agent import found existing NATS records with different material fields: "
            f"{conflicts}"
        )
    click.echo(
        "Imported legacy SQL agents: "
        f"{report.imported} imported, {report.skipped} skipped, 0 conflicts."
    )


@debug.command("migrate-accounts-to-oidc")
@click.argument("url")
@click.option(
    "--map",
    "maps",
    multiple=True,
    metavar="OLD=NEW",
    help="Explicit identity remap, e.g. --map alice=alice@example.com "
    "(repeatable; overrides --domain for the same OLD).",
)
@click.option(
    "--domain",
    default=None,
    metavar="DOMAIN",
    help="Append @DOMAIN to every bare (no-@) username, e.g. "
    "--domain example.com maps alice -> alice@example.com.",
)
@click.option(
    "--commit",
    is_flag=True,
    default=False,
    help="Apply the changes. Without this flag the command is a "
    "dry run that reports what would change and mutates nothing.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Allow merging onto a NEW id that already exists as a "
    "distinct user (merges admin rights). Off by default to avoid "
    "accidental privilege merges.",
)
def debug_migrate_accounts_to_oidc(
    url: str,
    maps: tuple[str, ...],
    domain: str | None,
    commit: bool,
    force: bool,
) -> None:
    """Remap user identities when switching the accounts provider to OIDC.

    The accounts provider keys users by username (``alice``); OIDC keys
    them by IdP email (``alice@example.com``). This rewrites every
    user-id-bearing row (permission grants, comments, policies, tokens,
    host ownership) so the team keeps its admin and data across the
    switch. Provider-agnostic: it touches only the database, so run it
    against your live DB *before* flipping ``OMNIGENT_AUTH_PROVIDER``.

    URL is a SQLAlchemy database URL, e.g.
    ``sqlite:////absolute/path/to/chat.db`` or
    ``postgresql://user:pass@host/dbname``.

    \b
    Examples:
      # Dry run: append the org domain to every username
      omnigent debug migrate-accounts-to-oidc sqlite:///chat.db --domain example.com
      # Apply it
      omnigent debug migrate-accounts-to-oidc sqlite:///chat.db --domain example.com --commit
      # Explicit per-user mapping (add --commit to apply)
      omnigent debug migrate-accounts-to-oidc sqlite:///chat.db --map alice=alice@corp.com

    \b
    IMPORTANT: always back up your database before running with
    --commit. The remap runs in one transaction but rewrites primary
    keys across several tables.
    """
    from sqlalchemy import create_engine

    from omnigent.server.identity_migration import build_domain_mapping, remap_identities

    engine = create_engine(url)
    try:
        mapping: dict[str, str] = {}
        if domain:
            mapping.update(build_domain_mapping(engine, domain))
        # Explicit --map pairs win over the domain-derived mapping.
        for pair in maps:
            if "=" not in pair:
                raise click.BadParameter(f"--map expects OLD=NEW, got {pair!r}")
            old, new = (part.strip() for part in pair.split("=", 1))
            if not old or not new:
                raise click.BadParameter(f"--map expects non-empty OLD=NEW, got {pair!r}")
            mapping[old] = new

        if not mapping:
            raise click.UsageError("nothing to migrate: pass --domain DOMAIN and/or --map OLD=NEW")

        report = remap_identities(engine, mapping, dry_run=not commit, force=force)
    finally:
        engine.dispose()

    mode = "COMMITTED" if report.committed else "DRY RUN (no changes written)"
    click.echo(f"\nIdentity remap — {mode}")
    click.echo(f"  database: {url}")
    click.echo(f"  mappings ({len(report.mapping)}):")
    for old, new in report.mapping.items():
        click.echo(f"    {old}  ->  {new}")

    # The NEW ids must equal what the IdP returns at login, or the user
    # signs in as a brand-new principal (not admin, no prior sessions).
    # This is the #1 footgun with --domain when the IdP email isn't
    # <username>@<domain> (e.g. GitHub returning a @gmail.com address).
    click.echo(
        "\n  ⚠ Each NEW id must match the email your IdP returns for that user.\n"
        "    If it doesn't, that user logs in as a new principal — re-add them to\n"
        "    the admin list, or re-run with --map OLD=<exact-idp-email>."
    )
    bare = sorted({new for new in report.mapping.values() if "@" not in new})
    if bare:
        click.echo(
            "    These targets have no '@' and are unlikely to be IdP emails: " + ", ".join(bare)
        )

    if report.per_table:
        click.echo("  rows changed:")
        for table, count in sorted(report.per_table.items()):
            click.echo(f"    {table}: {count}")
    else:
        click.echo("  rows changed: none")

    if report.skipped_missing:
        click.echo(f"  skipped (no user row): {', '.join(report.skipped_missing)}")
    if report.refused:
        click.echo(
            "  REFUSED (NEW id already exists — re-run with --force to merge): "
            + ", ".join(report.refused)
        )

    if not report.committed:
        click.echo("\nThis was a dry run. Re-run with --commit to apply.\n")
    else:
        click.echo("\nDone. Flip OMNIGENT_AUTH_PROVIDER=oidc and restart.\n")


def _workspace_mount_probe_matches(candidate: str, probe: httpx.Response) -> bool:
    """Whether a ``/api/2.0/omnigent`` mount probe answered like omnigent.

    :param candidate: The probed mount base URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :param probe: The ``GET <candidate>/v1/me`` response.
    :returns: ``True`` when the mount answered 200 (omnigent itself) or
        with a Databricks-fronted shape (302 to ``/oidc/`` or 401 with
        the ``DatabricksRealm`` challenge).
    """
    return probe.status_code == 200 or (
        _debug_databricks_workspace_login_target(candidate, probe) is not None
    )


def _cached_workspace_bearer(workspace_host: str) -> str | None:
    """Best-effort bearer for *workspace_host* from the OAuth cache.

    Unlike :func:`_databricks_workspace_token`, a missing ``databricks``
    extra is not an error here — probe callers simply fall back to
    unauthenticated behavior.

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :returns: A bearer token, or ``None`` when the ``databricks`` extra
        is not installed or no cached grant resolves for the host.
    """
    from omnigent.onboarding.databricks_config import databricks_sdk_installed

    if not databricks_sdk_installed():
        return None
    return _debug_databricks_workspace_token(workspace_host)


def _workspace_api_server_url(server: str) -> str:
    """Expand a bare Databricks workspace URL to its omnigent API base.

    ``https://<workspace>`` hosts serve the workspace web app at the
    root; workspace-hosted omnigent lives at ``/api/2.0/omnigent``.
    Users naturally paste the bare host, so when a path-less server URL
    answers like a Databricks workspace web app (a non-omnigent reply
    carrying the ``server: databricks`` header) AND the
    ``/api/2.0/omnigent`` mount answers like the API proxy, the
    expanded URL is adopted. Detection is behavioral — no hostname
    patterns — and URLs that already carry a path are returned
    untouched without any probe.

    Some workspace edges (Azure) answer the anonymous mount probe with
    a plain 404 — not the AWS proxy's 401-with-``DatabricksRealm``
    challenge — so a mount that works for authenticated callers is
    invisible to the anonymous probe. When the host-keyed Databricks
    OAuth cache holds a grant for the workspace (the user ran
    ``databricks auth login``), the mount probe is retried with that
    bearer before giving up.

    :param server: The user-supplied server URL, e.g.
        ``"https://example.databricks.com"``.
    :returns: The normalized base URL without a trailing slash, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"`` — or the
        input (normalized) when expansion does not apply.
    """
    from urllib.parse import urlsplit, urlunsplit

    import httpx as _httpx

    from omnigent.conversation_browser import WORKSPACE_API_PATH

    server = server.rstrip("/")
    parsed = urlsplit(server)
    if parsed.path not in ("", "/") or parsed.scheme != "https":
        return server
    try:
        probe = _httpx.get(f"{server}/v1/me", timeout=10.0)
    except _httpx.HTTPError:
        return server
    # Already something we understand at the root: an omnigent server
    # (200 / 401-with-login_url JSON) or a Databricks Apps edge /
    # API proxy (the login-target detector recognizes both).
    if probe.status_code == 200:
        return server
    if _debug_databricks_workspace_login_target(server, probe) is not None:
        return server
    server_header = probe.headers.get("server")
    if server_header is None or server_header.lower() != "databricks":
        return server
    candidate = urlunsplit((parsed.scheme, parsed.netloc, WORKSPACE_API_PATH, "", ""))
    try:
        api_probe = _httpx.get(f"{candidate}/v1/me", timeout=10.0)
    except _httpx.HTTPError:
        return server
    if _workspace_mount_probe_matches(candidate, api_probe):
        click.echo(f"Using {candidate} (Databricks workspace-hosted omnigent).")
        return candidate
    # The anonymous probe came back inconclusive (404 on Azure even
    # when the mount exists). Retry it with a cached workspace bearer;
    # either way, say what was decided — this branch is only reached
    # for genuine workspace web hosts, where a silent decline strands
    # the user on a bare URL that can only 404.
    token = _cached_workspace_bearer(server)
    if token is not None:
        try:
            authed_probe = _httpx.get(
                f"{candidate}/v1/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
        except _httpx.HTTPError:
            authed_probe = None
        if authed_probe is not None and _workspace_mount_probe_matches(candidate, authed_probe):
            click.echo(f"Using {candidate} (Databricks workspace-hosted omnigent).")
            return candidate
        click.echo(
            f"Note: {server} answers like a Databricks workspace, but "
            f"{candidate} did not answer as an omnigent server even with "
            f"the cached workspace credentials. Connecting to {server} as "
            "given; if omnigent is hosted on this workspace, refresh the "
            f"login with `databricks auth login --host {server}` or pass "
            "the full mount URL."
        )
        return server
    click.echo(
        f"Note: {server} answers like a Databricks workspace, but "
        f"{candidate} did not answer the anonymous probe "
        f"(HTTP {api_probe.status_code}). Some edges hide the mount from "
        "unauthenticated requests — if omnigent is hosted on this "
        f"workspace, run `databricks auth login --host {server}` and "
        "retry, or pass the full mount URL."
    )
    return server


def _debug_databricks_workspace_login_target(server: str, probe: httpx.Response) -> str | None:
    databricks_workspace_login_target = __facade_binding(
        "_databricks_workspace_login_target",
        _databricks_workspace_login_target,
    )
    return databricks_workspace_login_target(server, probe)


def _databricks_workspace_login_target(server: str, probe: httpx.Response) -> str | None:
    """Return the workspace host when *server* sits behind Databricks auth.

    Recognizes the two Databricks-fronted deployment shapes from the
    unauthenticated probe alone — no hostname pattern matching, so
    custom domains work too:

    - **Databricks Apps**: the Apps edge answers with a 302 to the
      fronting workspace's OIDC authorize endpoint
      (``https://<workspace>/oidc/oauth2/v2.0/authorize?...``); the
      redirect names the workspace to authenticate against.
    - **Workspace-hosted omnigent** (e.g.
      ``https://<workspace>/api/2.0/omnigent``): the workspace API
      proxy answers 401 with ``WWW-Authenticate: Bearer
      realm="DatabricksRealm"``; the workspace is the URL's own host.

    :param server: The server URL the user is logging in to, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :param probe: The unauthenticated ``GET /v1/me`` probe response.
    :returns: The workspace host, e.g.
        ``"https://example.databricks.com"``, or ``None`` when the
        response matches neither Databricks shape.
    """
    from urllib.parse import urlparse

    if probe.status_code in (302, 303, 307):
        raw_location = probe.headers.get("location")
        if raw_location is None:
            return None
        location = urlparse(raw_location)
        if location.scheme != "https" or not location.netloc:
            return None
        if not location.path.startswith("/oidc/"):
            return None
        return f"https://{location.netloc}"

    if probe.status_code == 401:
        www_authenticate = probe.headers.get("www-authenticate")
        if www_authenticate and "databricksrealm" in www_authenticate.lower():
            parsed = urlparse(server)
            if parsed.scheme == "https" and parsed.netloc:
                return f"https://{parsed.netloc}"

    return None


def _databricks_login(server: str, workspace_host: str) -> None:
    """Log in to a Databricks-fronted Omnigent server.

    Covers both Databricks Apps deployments and workspace-hosted
    omnigent (``https://<workspace>/api/2.0/omnigent``). Reuses an
    existing host-keyed Databricks CLI OAuth grant when one resolves;
    otherwise runs ``databricks auth login --host <workspace>``
    (browser flow). The minted token is verified against the server
    before anything is stored; a *cached* grant that fails
    verification (e.g. a stale token-cache entry minted for a
    different workspace) triggers one fresh browser login and a
    re-verify before failing loud. On success, a pointer record is
    stored in ``~/.omnigent/auth_tokens.json`` — no profile name is
    created or consulted anywhere.

    :param server: The server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :param workspace_host: The Databricks workspace to authenticate
        against, e.g. ``"https://example.databricks.com"``.
    :raises click.ClickException: When the ``databricks`` extra or CLI
        binary is missing, the workspace login fails, or the server
        rejects the workspace token.
    """
    from omnigent.onboarding.databricks_config import (
        DATABRICKS_EXTRA_INSTALL_HINT,
        databricks_sdk_installed,
    )

    click.echo(f"{server} authenticates via the Databricks workspace {workspace_host}.")

    if not databricks_sdk_installed():
        raise click.ClickException(
            "Logging in to a Databricks-fronted server (a Databricks App or "
            "workspace-hosted omnigent) requires the `databricks` extra "
            f"(databricks-sdk is not installed). Reinstall with:\n  "
            f"{DATABRICKS_EXTRA_INSTALL_HINT}"
        )

    token = _debug_databricks_workspace_token(workspace_host)
    fresh_login_done = False
    if token is None:
        token = _debug_login_and_mint_workspace_token(workspace_host)
        fresh_login_done = True

    # Verify the workspace token actually gets through the edge to THIS
    # server (the user may lack access to it), and learn our identity
    # for the success message.
    verify = _debug_verify_databricks_server_token(server, token)
    if verify.status_code != 200 and not fresh_login_done:
        # A cached grant can be stale or minted for a different
        # workspace (the CLI token cache is host-keyed but not
        # validated against the issuer). One fresh browser login
        # replaces the bad cache entry; then re-verify.
        click.echo(
            f"The cached Databricks credentials were rejected by {server} "
            f"(HTTP {verify.status_code}) — refreshing the workspace login."
        )
        token = _debug_login_and_mint_workspace_token(workspace_host)
        verify = _debug_verify_databricks_server_token(server, token)
    if verify.status_code != 200:
        raise click.ClickException(
            f"{workspace_host} accepted the login, but {server} rejected the token "
            f"(HTTP {verify.status_code}). Check that your user has access to this app."
        )
    user_id: str | None = None
    with contextlib.suppress(ValueError):
        raw_user = verify.json().get("user_id")
        user_id = raw_user if isinstance(raw_user, str) else None

    from omnigent.cli_auth import store_databricks_auth

    store_databricks_auth(
        server,
        workspace_host,
        user_id=user_id,
        # Workspace responses carry the org id; recorded so browser
        # links can append the ``?o=<org>`` workspace selector.
        org_id=verify.headers.get("x-databricks-org-id"),
    )
    who = f" as {user_id}" if user_id else ""
    click.echo(
        f"Logged in{who}. Commands targeting {server} now mint workspace tokens automatically."
    )


def _debug_login_and_mint_workspace_token(workspace_host: str) -> str:
    login_and_mint_workspace_token = __facade_binding(
        "_login_and_mint_workspace_token",
        _login_and_mint_workspace_token,
    )
    return login_and_mint_workspace_token(workspace_host)


def _login_and_mint_workspace_token(workspace_host: str) -> str:
    """Run the browser login for a workspace and mint a bearer from it.

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :returns: A fresh bearer token for the workspace.
    :raises click.ClickException: When the Databricks CLI binary is
        missing, the login exits non-zero, or no token resolves after
        a successful login.
    """
    _debug_run_databricks_browser_login(workspace_host)
    token = _debug_databricks_workspace_token(workspace_host)
    if token is None:
        raise click.ClickException(
            f"Workspace login completed but no token resolves for {workspace_host}. "
            f"Run `databricks auth token --host {workspace_host}` to debug."
        )
    return token


def _debug_run_databricks_browser_login(workspace_host: str) -> None:
    run_databricks_browser_login = __facade_binding(
        "_run_databricks_browser_login",
        _run_databricks_browser_login,
    )
    run_databricks_browser_login(workspace_host)


def _run_databricks_browser_login(workspace_host: str) -> None:
    """Run ``databricks auth login --host <workspace>`` (browser flow).

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :raises click.ClickException: When the Databricks CLI binary is
        missing or the login exits non-zero.
    """
    databricks_bin = shutil.which("databricks")
    if databricks_bin is None:
        raise click.ClickException(
            "The Databricks CLI is required to log in to a workspace. "
            "Install it first: https://docs.databricks.com/dev-tools/cli/install.html"
        )
    click.echo(f"Opening browser to log in to {workspace_host} ...")
    result = subprocess.run(
        [databricks_bin, "auth", "login", "--host", workspace_host],
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"`databricks auth login --host {workspace_host}` failed "
            f"(exit {result.returncode}). If the workspace is unreachable from "
            "this machine (VPN / IP access lists), resolve that and retry."
        )


def _debug_verify_databricks_server_token(server: str, token: str) -> httpx.Response:
    verify_databricks_server_token = __facade_binding(
        "_verify_databricks_server_token",
        _verify_databricks_server_token,
    )
    return verify_databricks_server_token(server, token)


def _verify_databricks_server_token(server: str, token: str) -> httpx.Response:
    """Probe ``GET /v1/me`` on *server* with a workspace bearer.

    :param server: The server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :param token: The workspace bearer token to present.
    :returns: The probe response (200 means the token is accepted and
        the body carries ``user_id``).
    :raises click.ClickException: When the server is unreachable.
    """
    import httpx as _httpx

    try:
        return _httpx.get(
            f"{server}/v1/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except _httpx.HTTPError as exc:
        raise click.ClickException(
            f"Could not reach {server}/v1/me to verify login: {exc}"
        ) from exc


def _debug_databricks_workspace_token(workspace_host: str) -> str | None:
    databricks_workspace_token = __facade_binding(
        "_databricks_workspace_token",
        _databricks_workspace_token,
    )
    return databricks_workspace_token(workspace_host)


def _databricks_workspace_token(workspace_host: str) -> str | None:
    """Mint a bearer for a workspace from the host-keyed OAuth cache.

    :param workspace_host: The workspace host, e.g.
        ``"https://example.databricks.com"``.
    :returns: A bearer token, or ``None`` when no cached grant
        resolves (the caller should run ``databricks auth login``).
    """
    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _resolve_databricks_auth,
    )

    try:
        auth, _host = _resolve_databricks_auth(host=workspace_host)
        return auth.current_token()
    except (DatabricksAuthError, ValueError):
        return None
