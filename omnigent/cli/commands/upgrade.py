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

@cli.command("upgrade")
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Report whether a newer release is available, without upgrading. "
    "Exits non-zero when a newer release exists.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Stop in-flight sessions immediately instead of waiting for them to drain.",
)
@click.option(
    "--pre",
    "pre",
    is_flag=True,
    help="Consider pre-releases (e.g. release candidates), and pass the "
    "installer's allow-pre-releases flag. Useful for validating a TestPyPI rc.",
)
def upgrade(check_only: bool, force: bool, pre: bool) -> None:
    """Upgrade the omnigent CLI to the latest release on PyPI.

    Detects how omnigent was installed (uv / pip / pipx / poetry), checks
    the configured index for a newer release and — unless ``--check`` —
    drains and stops the local background server and host daemon, then runs
    the matching upgrade command. The next ``omni`` invocation starts a
    fresh server on the new code automatically (via the version-aware
    config signature), so no explicit restart is needed.

    In-flight agent sessions are waited on by default; pass ``--force`` to
    stop them immediately. Pass ``--pre`` to consider pre-releases (rc /
    beta) — handy for validating a TestPyPI candidate against your
    configured index. Source checkouts / editable installs are not upgraded
    here — update those with ``git pull``.

    :param check_only: Only report availability; do not upgrade. Exits
        with status 1 when a newer release exists.
    :param force: Stop in-flight sessions immediately rather than draining.
    :param pre: Consider pre-releases and allow the installer to fetch them.
    :returns: None.
    """
    import importlib.metadata

    from packaging.version import InvalidVersion, parse

    from omnigent.update_check import (
        _build_upgrade_suggestion,
        _find_repo_root,
        _read_installed_wheel_info,
        _run_upgrade_command,
        fetch_latest_version,
    )

    # Source checkout / editable install — there's no released wheel to
    # swap in place; the correct update path is git, not a reinstall.
    if _find_repo_root() is not None:
        raise click.ClickException(
            "This is a source checkout — update it with `git pull` (and reinstall "
            "dependencies), not `omni upgrade`."
        )
    info = _read_installed_wheel_info()
    if info is None:
        raise click.ClickException(
            "Couldn't determine how omnigent is installed; upgrade it manually."
        )
    if info.is_editable:
        raise click.ClickException(
            "This is an editable install — update it with `git pull`, not `omni upgrade`."
        )

    current = importlib.metadata.version("omnigent")
    latest = fetch_latest_version(include_prereleases=pre)
    if latest is None:
        raise click.ClickException(
            "Couldn't reach the package index to check for a newer release. Check your "
            "connection (or OMNIGENT_INDEX_URL / your configured index) and try again."
        )
    try:
        is_behind = parse(latest) > parse(current)
    except InvalidVersion:
        is_behind = latest != current

    if not is_behind:
        click.echo(f"omnigent is up to date (v{current}).")
        return

    click.echo(f"A new release is available: v{current} → v{latest}.")
    if check_only:
        # Non-zero so scripts/CI can gate on "an upgrade is available".
        # SystemExit (not ctx.exit) because main() runs the group with
        # standalone_mode=False, where ctx.exit's code is returned and
        # dropped rather than applied — SystemExit propagates correctly.
        raise SystemExit(1)

    suggestion = _build_upgrade_suggestion(info, allow_prerelease=pre)
    if not suggestion.runnable:
        raise click.ClickException(
            f"No automatic upgrade command is known for this install. {suggestion.command}."
        )

    # Drain (or force-stop) the local server + daemon BEFORE swapping the
    # code, so the running process never serves half-upgraded modules.
    # The next command respawns a fresh server on the new version.
    if not force:
        _wait_for_local_sessions_to_drain()
    if _stop_local_server_and_daemon(force=force):
        click.echo("Stopped the background server before upgrading.")

    console = Console()
    code = _run_upgrade_command(suggestion.command, console)
    if code != 0:
        raise click.ClickException(
            f"Upgrade command exited with status {code}; your previous install is intact."
        )
    click.echo(
        f"✓ Upgraded to v{latest}. Re-run your command — the local server will "
        "start on the new version."
    )


def _bundle(source: Path) -> bytes:
    """
    Produce a tar.gz bundle from a directory or standalone
    Omnigent YAML file, or pass through an existing tarball.

    Environment variable references (``${VAR}``) in
    ``config.yaml`` and ``tools/mcp/*.yaml`` are expanded
    using the client's environment before bundling. This
    ensures the server receives resolved secrets rather
    than unresolved ``${VAR}`` references it cannot
    resolve.

    :param source: Path to an agent image directory,
        standalone Omnigent YAML file, or an existing
        ``.tar.gz`` bundle file.
    :returns: The gzipped tarball bytes.
    :raises OmnigentError: If a required env var is
        missing during expansion.
    """
    import io
    import tarfile

    if source.is_file() and source.suffix.lower() in {".yaml", ".yml"}:
        from omnigent.spec import materialize_bundle

        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_dir = materialize_bundle(source, Path(tmpdir) / "bundle")
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for file_path in bundle_dir.rglob("*"):
                    if file_path.is_file():
                        tf.add(str(file_path), arcname=str(file_path.relative_to(bundle_dir)))
            return buf.getvalue()

    if source.is_file():
        return source.read_bytes()

    # Pre-resolve env vars in YAML files that contain secrets.
    resolved = _resolve_bundle_env_vars(source)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for file_path in source.rglob("*"):
            if file_path.is_file():
                arcname = str(file_path.relative_to(source))
                if arcname in resolved:
                    # Write the resolved YAML instead of the
                    # original file (which has ${VAR} refs).
                    data = resolved[arcname].encode("utf-8")
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
                else:
                    tf.add(str(file_path), arcname=arcname)
    return buf.getvalue()


def _resolve_bundle_env_vars(source: Path) -> dict[str, str]:
    """
    Expand ``${VAR}`` references in YAML files that contain
    secrets, using the client's environment.

    Returns a mapping of ``arcname → resolved YAML text`` for
    files that were modified. Files without env var references
    are omitted (bundled as-is).

    Expanded fields:

    - ``config.yaml``: ``llm.connection.*`` and
      ``executor.connection.*`` values, ``executor.auth``
      ``api_key`` / ``base_url`` (when ``type: api_key``), and
      ``tools.builtins[*]`` dict-entry values (except ``name``)
    - ``tools/mcp/*.yaml``: ``headers.*`` and ``env.*`` values

    These mirror the server-side parser's ``${VAR}`` expansion
    sites. Resolving here, against the client's own environment,
    is what keeps secrets working now that the server refuses to
    expand tenant-uploaded bundles against its process env.

    :param source: The agent image directory.
    :returns: ``{arcname: resolved_yaml_text}`` for files
        that had env vars expanded.
    :raises OmnigentError: If a ``${VAR}`` reference
        cannot be resolved from the environment.
    """
    from omnigent.spec import expand_env_vars

    resolved: dict[str, str] = {}

    # ── config.yaml ──────────────────────────────────
    config_path = source / "config.yaml"
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text())
        if isinstance(raw, dict):
            changed = _expand_config_env_vars(raw, expand_env_vars)
            if changed:
                resolved["config.yaml"] = yaml.dump(
                    raw,
                    default_flow_style=False,
                )

    # ── tools/mcp/*.yaml ─────────────────────────────
    # ``headers`` (HTTP transport auth) and ``env`` (stdio transport
    # process env) are both secret-bearing and both expanded by the
    # server-side parser, so resolve both client-side.
    mcp_dir = source / "tools" / "mcp"
    if mcp_dir.is_dir():
        for yaml_file in sorted(mcp_dir.glob("*.yaml")):
            raw = yaml.safe_load(yaml_file.read_text())
            if not isinstance(raw, dict):
                continue
            changed = False
            for field in ("headers", "env"):
                value = raw.get(field)
                if isinstance(value, dict):
                    raw[field] = expand_env_vars(
                        {str(k): str(v) for k, v in value.items()},
                    )
                    changed = True
            if changed:
                arcname = str(yaml_file.relative_to(source))
                resolved[arcname] = yaml.dump(
                    raw,
                    default_flow_style=False,
                )

    return resolved


class _LLMDeploy(BaseModel):  # type: ignore[explicit-any]  # Pydantic extra="allow" stubs use Any
    """
    Pydantic model for the ``llm:`` block during deploy-time
    env var expansion.

    :param connection: Key-value pairs for LLM connection
        config, e.g. ``{"api_key": "${OPENAI_API_KEY}"}``.
    """

    model_config = ConfigDict(extra="allow")
    connection: dict[str, str] | None = None


class _BuiltinEntry(BaseModel):  # type: ignore[explicit-any]  # Pydantic extra="allow" stubs use Any
    """
    Pydantic model for a single dict entry in
    ``tools.builtins`` during deploy-time env var expansion.

    :param name: The built-in tool name, e.g.
        ``"web_search"``.
    """

    model_config = ConfigDict(extra="allow")
    name: str


class _ToolsDeploy(BaseModel):  # type: ignore[explicit-any]  # builtins field is list[str | dict[str, Any]]
    """
    Pydantic model for the ``tools:`` block during deploy-time
    env var expansion.

    :param builtins: Mixed list of string tool names and dict
        entries with config fields, e.g.
        ``["web_search", {"name": "web_search",
        "api_key": "${KEY}"}]``.
    """

    model_config = ConfigDict(extra="allow")
    builtins: list[str | dict[str, Any]] | None = None  # type: ignore[explicit-any]


class _ExecutorDeploy(BaseModel):  # type: ignore[explicit-any]  # auth is a free-form mapping
    """
    Pydantic model for the ``executor:`` block during deploy-time
    env var expansion.

    Mirrors the secret-bearing fields the server-side parser
    expands (``omnigent/spec/parser.py`` — ``_parse_executor`` /
    ``_parse_executor_auth``): the ``connection`` dict and, for
    ``auth.type == "api_key"``, the ``api_key`` / ``base_url``
    values. Resolving these client-side keeps ``${VAR}`` working
    for operator specs now that the server no longer expands
    tenant bundles.

    :param connection: Key-value pairs for executor connection
        config, e.g. ``{"api_key": "${OPENAI_API_KEY}"}``.
    :param auth: The ``auth:`` mapping, e.g.
        ``{"type": "api_key", "api_key": "${OPENAI_API_KEY}"}``.
        Only expanded when ``type == "api_key"``.
    """

    model_config = ConfigDict(extra="allow")
    connection: dict[str, str] | None = None
    auth: dict[str, Any] | None = None  # type: ignore[explicit-any]


class _DeployConfig(BaseModel):  # type: ignore[explicit-any]  # Pydantic extra="allow" stubs use Any
    """
    Pydantic model for the top-level config.yaml structure
    during deploy-time env var expansion.

    Only the fields containing secrets (``llm``, ``executor``,
    ``tools``) are modeled; all other fields pass through via
    ``extra="allow"``.

    :param llm: The LLM configuration block, or ``None``
        if absent.
    :param executor: The executor configuration block, or
        ``None`` if absent.
    :param tools: The tools configuration block, or ``None``
        if absent.
    """

    model_config = ConfigDict(extra="allow")
    llm: _LLMDeploy | None = None
    executor: _ExecutorDeploy | None = None
    tools: _ToolsDeploy | None = None


def _expand_config_env_vars(  # type: ignore[explicit-any]  # raw is parsed YAML (heterogeneous values)
    raw: dict[str, Any],
    expand_fn: Callable[[dict[str, str]], dict[str, str]],
) -> bool:
    """
    Expand ``${VAR}`` references in-place in a parsed
    ``config.yaml`` dict. Returns ``True`` if any field
    was expanded.

    Expanded fields (mirrors the server-side parser's expansion
    sites so operator specs resolve identically client-side now
    that the server no longer expands tenant bundles):

    - ``llm.connection`` — all values
    - ``executor.connection`` — all values
    - ``executor.auth`` — ``api_key`` / ``base_url`` when
      ``type == "api_key"``
    - ``tools.builtins[*]`` — dict-entry values except ``name``

    :param raw: The parsed config.yaml dict (modified in-place).
    :param expand_fn: Callable that expands env var references
        in a string-to-string dict, e.g.
        :func:`omnigent.spec.expand_env_vars`.
    :returns: ``True`` if any values were expanded.
    """
    cfg = _DeployConfig.model_validate(raw)
    changed = False

    if cfg.llm is not None and cfg.llm.connection is not None:
        raw["llm"]["connection"] = expand_fn(cfg.llm.connection)
        changed = True

    if cfg.executor is not None and cfg.executor.connection is not None:
        raw["executor"]["connection"] = expand_fn(cfg.executor.connection)
        changed = True

    # ``executor.auth`` with ``type: api_key`` — only ``api_key`` and
    # ``base_url`` are secret-bearing (matches _parse_executor_auth).
    if (
        cfg.executor is not None
        and cfg.executor.auth is not None
        and cfg.executor.auth.get("type") == "api_key"
    ):
        auth_secrets = {
            k: str(cfg.executor.auth[k])
            for k in ("api_key", "base_url")
            if cfg.executor.auth.get(k) is not None
        }
        if auth_secrets:
            raw["executor"]["auth"].update(expand_fn(auth_secrets))
            changed = True

    if cfg.tools is not None and cfg.tools.builtins is not None:
        changed = (
            _expand_builtin_env_vars(
                raw["tools"]["builtins"],
                cfg.tools.builtins,
                expand_fn,
            )
            or changed
        )

    return changed


def _expand_builtin_env_vars(  # type: ignore[explicit-any]  # entries are parsed YAML dicts
    raw_builtins: list[str | dict[str, Any]],
    parsed_builtins: list[str | dict[str, Any]],
    expand_fn: Callable[[dict[str, str]], dict[str, str]],
) -> bool:
    """
    Expand ``${VAR}`` references in dict entries of
    ``tools.builtins``, modifying *raw_builtins* in-place.

    String entries are skipped (no config to expand). Dict
    entries have all fields except ``name`` expanded.

    :param raw_builtins: The mutable builtins list from the
        raw config dict (modified in-place).
    :param parsed_builtins: The Pydantic-parsed builtins list
        used for typed access.
    :param expand_fn: Callable that expands env var references
        in a string-to-string dict.
    :returns: ``True`` if any values were expanded.
    """
    changed = False
    for i, entry in enumerate(parsed_builtins):
        if not isinstance(entry, dict):
            continue
        parsed = _BuiltinEntry.model_validate(entry)
        # Extra fields are the tool-specific config (api_key, etc.).
        config_fields = (
            {str(k): str(v) for k, v in parsed.model_extra.items()} if parsed.model_extra else {}
        )
        if config_fields:
            expanded = expand_fn(config_fields)
            raw_builtins[i] = {"name": parsed.name, **expanded}
            changed = True
    return changed


# Click ``flag_value`` for bare ``--resume`` (no arg). Must exist
# before any command's decorator evaluates.
_RESUME_PICKER_SENTINEL = "__resume_picker__"


