from __future__ import annotations

from typing import Any

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


def _login_workspace_api_server_url(server: str) -> str:
    from .debug import _workspace_api_server_url as fallback

    workspace_api_server_url = __facade_binding("_workspace_api_server_url", fallback)
    return workspace_api_server_url(server)


def _login_databricks_workspace_login_target(server: str, probe: Any) -> str | None:
    from .debug import _databricks_workspace_login_target as fallback

    databricks_workspace_login_target = __facade_binding(
        "_databricks_workspace_login_target",
        fallback,
    )
    return databricks_workspace_login_target(server, probe)


def _login_databricks_login(server: str, workspace_host: str) -> None:
    from .debug import _databricks_login as fallback

    databricks_login = __facade_binding("_databricks_login", fallback)
    databricks_login(server, workspace_host)


def _login_accounts_login(server: str) -> None:
    accounts_login = __facade_binding("_accounts_login", _accounts_login)
    accounts_login(server)


@cli.command("login")
@click.argument("server_url")
def login(server_url: str) -> None:
    """Authenticate with a remote Omnigent server.

    Probes the server's auth mode and runs the matching flow:

    \b
    - accounts mode: prompts for username + password (no browser
      needed), POSTs ``/auth/login``, stores the session JWT in
      ``~/.omnigent/auth_tokens.json`` keyed by server URL.
    - OIDC mode: opens the browser, polls the CLI ticket endpoint,
      stores the session JWT when the browser flow completes.
    - header mode: no login needed (proxy injects identity); we
      print a hint and exit successfully.
    - Databricks-fronted (a Databricks App, or omnigent hosted on
      a workspace API path): detected from the probe response — we
      log in to the workspace via ``databricks auth login --host
      <workspace>`` (browser) and store a pointer record so later
      commands mint fresh workspace tokens automatically. Requires
      the ``databricks`` extra.

    Subsequent ``omnigent run --server <url>`` commands then
    use the stored token via the runner / host-tunnel auth chain.

    \b
    Example:
      omnigent login http://localhost:6767
      omnigent run --server http://localhost:6767

    :param server_url: The remote server URL, e.g.
        ``"http://localhost:6767"``.
    """
    import httpx as _httpx

    # A bare Databricks workspace URL means its /api/2.0/omnigent mount.
    server = _login_workspace_api_server_url(server_url.rstrip("/"))

    # ── Step 0: Probe the server's auth mode. ──────────────────
    # /v1/me returns a JSON ``login_url`` on 401 — "/login" for
    # accounts, "/auth/login" for OIDC, and no login_url at all
    # for header mode. A 302 to a workspace OAuth page (Databricks
    # Apps) or a 401 with a DatabricksRealm challenge (workspace-
    # hosted omnigent) means Databricks fronts the server. This
    # lets one CLI command handle every posture without a flag.
    try:
        probe = _httpx.get(f"{server}/v1/me", timeout=10.0)
    except _httpx.HTTPError as exc:
        raise click.ClickException(
            f"Could not reach {server}/v1/me: {exc}\nIs the server running?"
        ) from exc

    databricks_workspace = _login_databricks_workspace_login_target(server, probe)
    if databricks_workspace is not None:
        _login_databricks_login(server, databricks_workspace)
        return

    detected_login_url: str | None = None
    if probe.status_code == 401:
        import contextlib as _contextlib

        # 401 with non-JSON body — probably not an Omnigent server.
        # Suppress: we fall through to the OIDC path below which has
        # its own clearer error message.
        with _contextlib.suppress(ValueError):
            detected_login_url = probe.json().get("login_url")
    elif probe.status_code == 200:
        # Header mode (or already authenticated). Tell the user
        # they don't need to log in and exit cleanly.
        click.echo(
            f"{server} is in header-auth mode — no login needed. "
            "The proxy in front of it injects your identity on every "
            "request."
        )
        return

    if detected_login_url == "/login":
        _login_accounts_login(server)
        return

    # Fall through: OIDC mode (or unknown — let the ticket endpoint's
    # error message guide the user).
    import webbrowser

    from omnigent.cli_auth import store_token

    # Step 1: Request a CLI login ticket.
    try:
        resp = _httpx.post(f"{server}/auth/cli-login", timeout=10.0)
        resp.raise_for_status()
    except _httpx.HTTPError as exc:
        raise click.ClickException(
            f"Could not reach {server}/auth/cli-login: {exc}\n"
            f"Is the server running with OMNIGENT_AUTH_PROVIDER=oidc?"
        ) from exc

    data = resp.json()
    ticket = data["ticket"]
    login_url = f"{server}{data['login_url']}"

    # Step 2: Open the browser.
    click.echo(f"Opening browser for login: {login_url}")
    click.echo("Waiting for authentication...")
    webbrowser.open(login_url)

    # Step 3: Poll until the ticket is fulfilled or expired.
    poll_url = f"{server}/auth/cli-poll?ticket={ticket}"
    import time as _time

    deadline = _time.time() + _CLI_LOGIN_TIMEOUT_SECONDS
    while _time.time() < deadline:
        _time.sleep(2)
        try:
            poll_resp = _httpx.get(poll_url, timeout=10.0)
        except _httpx.HTTPError:
            continue

        if poll_resp.status_code == 202:
            # Still pending.
            continue
        if poll_resp.status_code == 200:
            result = poll_resp.json()
            token = result["token"]
            user_id = result["user_id"]
            expires_in = result.get("expires_in", 8 * 3600)
            store_token(
                server_url=server,
                token=token,
                user_id=user_id,
                expires_at=_time.time() + expires_in,
            )
            click.echo(f"Logged in as {user_id}")
            return
        # 410 or other error — ticket expired.
        raise click.ClickException("Login ticket expired or was rejected. Please try again.")

    raise click.ClickException(
        "Login timed out — the browser flow was not completed "
        f"within {_CLI_LOGIN_TIMEOUT_SECONDS} seconds."
    )


_CLI_LOGIN_TIMEOUT_SECONDS = 300  # 5 minutes


def _accounts_login(server: str) -> None:
    """Run the accounts-mode login flow: prompt + POST /auth/login.

    No browser, no polling — accounts auth is username + password,
    we just collect them, send them, and store the returned JWT.

    Three failure paths surface as ClickExceptions so the click
    error formatter renders them consistently with the rest of
    the CLI:

    - Network failure on /auth/login → connection error.
    - 401 from /auth/login → "invalid username or password"
      (the server's generic message — we don't reveal whether
      the username was unknown or the password was wrong).
    - 5xx → "server error".

    On success, the session JWT goes to
    ``~/.omnigent/auth_tokens.json`` via the existing
    :func:`omnigent.cli_auth.store_token`. From there both
    ``omnigent run`` and ``omnigent host`` pick it up
    automatically when they call ``--server <url>``.
    """
    import httpx as _httpx

    from omnigent.cli_auth import store_token

    click.echo(f"Signing in to {server} (accounts auth).")
    # `admin` is the bootstrap username; prefill to match what
    # the web LoginPage does.
    username = click.prompt("Username", default="admin")
    password = click.prompt("Password", hide_input=True)

    try:
        resp = _httpx.post(
            f"{server}/auth/login",
            json={"username": username, "password": password},
            timeout=10.0,
        )
    except _httpx.HTTPError as exc:
        raise click.ClickException(f"Could not reach {server}/auth/login: {exc}") from exc

    if resp.status_code == 401:
        # Generic message — matches what the server returns and
        # what the web form shows. Don't echo the username back
        # in case the terminal is being recorded / shared.
        raise click.ClickException("Invalid username or password.")
    if resp.status_code >= 500:
        raise click.ClickException("Server error during login. Try again in a moment.")
    if not resp.is_success:
        raise click.ClickException(f"Login failed ({resp.status_code}): {resp.text[:200]}")

    body = resp.json()
    token = body["token"]
    user_id = body["user"]["id"]
    expires_in = body.get("expires_in", 8 * 3600)

    import time as _time

    store_token(
        server_url=server,
        token=token,
        user_id=user_id,
        expires_at=_time.time() + expires_in,
    )
    click.echo(f"Logged in as {user_id}.")


# Direction codes used by ``pane-split`` and ``pane-picker``.
# ``"v"`` = vertical split (new pane stacked below; tmux ``-v``).
# ``"h"`` = horizontal split (new pane side-by-side; tmux ``-h``).
# ``"w"`` = new window/tab (tmux ``new-window``).
_PANE_SPLIT_DIRECTIONS = ("v", "h", "w")
