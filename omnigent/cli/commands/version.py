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

@cli.command(hidden=True)
def version() -> None:
    """Print the installed Omnigent version."""
    print(_format_version())


def _parse_config_settings(
    settings: tuple[str, ...],
    *,
    resolve_paths: bool = False,
) -> dict[str, str | bool]:
    """
    Parse and validate ``KEY=VALUE`` pairs from the ``config`` command.

    Raises :class:`click.ClickException` for malformed items or unknown keys.

    :param settings: Raw ``KEY=VALUE`` strings, e.g.
        ``("default_agent=examples/hello.yaml", "model=gpt-5.4-mini")``.
    :param resolve_paths: When ``True``, resolve relative ``default_agent``
        paths to absolute so the config works regardless of working directory.
        Set for ``--global`` writes; leave ``False`` for project-local writes
        where the path is intentionally relative to the project root.
    :returns: Validated mapping of config key → value, e.g.
        ``{"agent": "examples/hello.yaml", "model": "gpt-5.4-mini"}``.
    """
    parsed: dict[str, str | bool] = {}
    for item in settings:
        if "=" not in item:
            raise click.ClickException(
                f"Expected KEY=VALUE, got: {item!r}. "
                "Example: omnigent config set --global default_agent=myagent.yaml"
            )
        key, _, value = item.partition("=")
        if key not in _GLOBAL_CONFIG_KEYS:
            raise click.ClickException(
                f"Unknown config key {key!r}. "
                f"Supported keys: {', '.join(sorted(_GLOBAL_CONFIG_KEYS))}"
            )
        # Resolve ``default_agent`` to an absolute path so ``omnigent`` works from
        # any working directory, not just the directory where config was set.
        if (
            resolve_paths
            and key == "default_agent"
            and not value.startswith(("http://", "https://"))
        ):
            value = str(Path(value).resolve())
        if key in _BOOLEAN_CONFIG_KEYS:
            parsed[key] = _parse_config_bool(key, value)
        else:
            parsed[key] = value
    return parsed


def _validate_unset_keys(unset_keys: tuple[str, ...]) -> list[str]:
    """
    Validate keys passed to ``--unset`` against ``_GLOBAL_CONFIG_KEYS``.

    Raises :class:`click.ClickException` for any unrecognised key.

    :param unset_keys: Keys to remove from global config, e.g.
        ``("server",)``.
    :returns: The same keys as a list, confirming they are all valid.
    """
    validated: list[str] = []
    for key in unset_keys:
        if key not in _GLOBAL_CONFIG_KEYS:
            raise click.ClickException(
                f"Unknown config key {key!r}. "
                f"Supported keys: {', '.join(sorted(_GLOBAL_CONFIG_KEYS))}"
            )
        validated.append(key)
    return validated


def _print_config_defaults() -> None:
    """Print the effective CLI defaults (user + project-level).

    The ``KEY=VALUE`` defaults from ``~/.omnigent/config.yaml`` (user) and
    ``.omnigent/config.yaml`` in the cwd (project, takes precedence).
    Used by ``omnigent config list``.

    :returns: None. Side effect: writes to stdout.
    """
    # Only the user-facing run defaults (the keys ``config set`` accepts).
    # Internal blocks (``providers``, ``host``, ``tui``) are omitted — the
    # ``providers`` block is shown in the credentials-by-harness section.
    global_cfg = {k: v for k, v in _load_global_config().items() if k in _GLOBAL_CONFIG_KEYS}
    local_cfg = {k: v for k, v in _load_local_config().items() if k in _GLOBAL_CONFIG_KEYS}
    if not global_cfg and not local_cfg:
        click.echo(
            "  (none set — `omnigent config set key=value` for project,\n"
            "   or `omnigent config set --global key=value` for user-level)"
        )
        return
    global_path = _effective_global_config_path()
    local_path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    # When the cwd IS the home directory, the project-level path
    # (``cwd/.omnigent/config.yaml``) resolves to the SAME file as the
    # user-level path (``~/.omnigent/config.yaml``). Dedup on the resolved
    # absolute path so the one file is shown once, not twice under two
    # spellings. ``resolve()`` collapses ``~`` and symlinks for the compare.
    local_is_global = local_cfg and local_path.resolve() == global_path.resolve()
    if global_cfg:
        click.echo(f"  # {_display_config_path(global_path)}")
        for k, v in sorted(global_cfg.items()):
            click.echo(f"  {k}={v}")
    if local_cfg and not local_is_global:
        click.echo(f"  # {local_path}")
        for k, v in sorted(local_cfg.items()):
            click.echo(f"  {k}={v}")


