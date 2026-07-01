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

@cli.command("setup")
@click.option(
    "--internal-beta/--no-internal-beta",
    default=False,
    help="Run the standard model/credential setup (default): choose a "
    "provider for each harness and set your defaults. Pass --internal-beta "
    "to configure Databricks internal-beta defaults and authentication.",
)
def setup(internal_beta: bool) -> None:
    """
    Launch the Omnigent first-time setup flow.

    By default this runs the standard model/credential picker — choose a
    provider for each harness and set your defaults, then start a session
    with ``omnigent run``. (List configured credentials with
    ``omnigent config list``.) Pass ``--internal-beta`` to configure
    Databricks internal-beta defaults and authentication instead.
    """
    if internal_beta:
        # The internal-beta workspace defaults are excluded from the public OSS
        # build. Fail loud with a clear message instead of an ImportError deep
        # in the onboarding flow when someone passes --internal-beta there.
        try:
            import omnigent.onboarding.internal_beta  # noqa: F401
        except ImportError:
            raise click.ClickException(
                "Databricks internal-beta setup is not available in this build. "
                "Run `omnigent setup` for the standard model/credential setup."
            ) from None
        # Internal-beta routing mints workspace OAuth tokens via
        # databricks-sdk at runtime, and the SDK ships in the `databricks`
        # extra rather than the default install. Fail loud up front instead
        # of completing the whole login flow and breaking on the first turn.
        from omnigent.onboarding.databricks_config import (
            DATABRICKS_EXTRA_INSTALL_HINT,
            databricks_sdk_installed,
        )

        if not databricks_sdk_installed():
            raise click.ClickException(
                "Databricks internal-beta setup needs the databricks extra "
                f"(databricks-sdk). Reinstall with:\n  {DATABRICKS_EXTRA_INSTALL_HINT}"
            )
        # Surface missing external tooling (Node, tmux) before the Databricks
        # bootstrap so a fresh machine sees every gap at once.
        _warn_missing_harness_dependencies()
        from omnigent.onboarding.internal_beta import _INTERNAL_BETA_DEFAULT_SERVER
        from omnigent.onboarding.sandboxes.lakebox import install_demo_databricks_cli
        from omnigent.onboarding.setup import run_onboarding

        # Install the demo `databricks` CLI (with the `lakebox`
        # subcommand) BEFORE profile onboarding — `run_onboarding`
        # shells out to `databricks auth login`, and a fresh machine
        # might not have the binary on PATH at all. Idempotent: skips
        # the installer when the demo CLI is already present, but
        # still persists ~/.local/bin in the user's shell rc files.
        install_demo_databricks_cli()
        with _isolated_databricks_cfg():
            if not run_onboarding():
                raise click.ClickException("onboarding did not complete; see output above.")
            _run_configure_databricks()
        agent_path = _materialize_internal_beta_agents()
        _save_global_config(
            {
                "default_agent": str(agent_path),
                "profile": "oss",
                "server": _INTERNAL_BETA_DEFAULT_SERVER,
                # auth: block provides the default executor credentials for
                # agents that do not declare executor.auth themselves.
                "auth": {"type": "databricks", "profile": "oss"},
            }
        )
        click.echo(f"Set default_agent={agent_path} in {_GLOBAL_CONFIG_PATH}")
        click.echo("Type `omnigent claude` to get started with Claude Code on omnigent.")
        return

    # --no-internal-beta: the standard model/credential picker. It warns
    # about missing Node/tmux itself, configures providers/defaults, and
    # returns; the user then starts a session with ``omnigent run``.
    _run_configure_harnesses_interactive()


# ─── sandbox group ────────────────────────────────────────────────
# The provider-agnostic sandbox CLI lives in omnigent/cli_sandbox.py.
# Provider launcher modules are optional and may be absent from a given
# distribution; hide the group when none are available.
# `omnigent lakebox` is kept as an alias for `omnigent sandbox …
# --provider lakebox`, registered only when the lakebox provider ships.
if _sandbox_providers():
    cli.add_command(_sandbox_group)
    if "lakebox" in _sandbox_providers():
        cli.add_command(_lakebox_alias_group)

# ─── debug group ──────────────────────────────────────────────────
#
# Operator-only maintenance commands, grouped under ``omnigent debug``
# so they stay out of the everyday surface.
#
# ``db-upgrade`` runs manual schema operations on an Omnigent tracking
# database. Mirrors ``mlflow db upgrade`` (``mlflow/db.py``) so the
# workflow is familiar to anyone who's bumped an MLflow database before.
# The server initializes a fresh database on first boot and attempts to
# auto-upgrade an existing database that is behind head; this command
# remains available for explicit/manual upgrades, or for retrying an
# automatic migration that failed.
#
# ``migrate-accounts-to-oidc`` remaps user identities when switching the
# built-in accounts provider to OIDC.


