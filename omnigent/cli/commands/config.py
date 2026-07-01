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

@cli.group("config", cls=_ConfigGroup)
def config_grp() -> None:
    """Get, set, and view Omnigent defaults and credentials.

    Defaults (auto_open_conversation, default_agent, harness, model,
    server) are used by ``omnigent run``. Project-level config
    (``.omnigent/config.yaml`` in the cwd, like ``.git/config``) overrides
    user-level config (``~/.omnigent/config.yaml``, like ``~/.gitconfig``).

    \b
    Subcommands:
      list   Show the effective defaults + configured credentials (by harness).
      set    Set one or more defaults (KEY=VALUE).
      unset  Remove one or more defaults.
    """


@config_grp.command("list")
def config_list() -> None:
    """List the effective defaults and configured credentials.

    Prints the defaults (user + project), then the configured model
    credentials grouped by harness with each harness's default marked — the
    merged view of everything ``omnigent run`` will use (including
    ambient-detected credentials).

    :returns: None.
    """
    click.echo("Defaults")
    _print_config_defaults()
    click.echo()
    _print_credentials_by_harness()


@config_grp.command("set")
@click.option(
    "--global",
    "is_global",
    is_flag=True,
    default=False,
    help="Write to ~/.omnigent/config.yaml (user-level) instead of the project config.",
)
@click.argument("settings", nargs=-1, required=True, metavar="KEY=VALUE...")
def config_set(is_global: bool, settings: tuple[str, ...]) -> None:
    """Set one or more Omnigent defaults.

    Without ``--global``, pairs are written to ``.omnigent/config.yaml``
    in the current directory (project-level, like ``.git/config``); with
    ``--global`` to ``~/.omnigent/config.yaml`` (user-level, like
    ``~/.gitconfig``). Project values take precedence.

    Supported keys: auto_open_conversation, default_agent, harness,
    model, server.

    :param is_global: When ``True``, write to ``~/.omnigent/config.yaml``;
        when ``False``, to ``.omnigent/config.yaml`` in cwd.
    :param settings: ``KEY=VALUE`` pairs to set, e.g.
        ``("default_agent=examples/hello.yaml", "model=gpt-5.4-mini")``.

    \b
    Examples:
      omnigent config set default_agent=examples/hello_world.yaml
      omnigent config set --global server=https://<app>.databricksapps.com
    """
    if is_global:
        parsed = _parse_config_settings(settings, resolve_paths=True)
        _save_global_config(parsed, ())
        config_path: Path = _effective_global_config_path()
    else:
        parsed = _parse_config_settings(settings, resolve_paths=False)
        _save_local_config(parsed, ())
        config_path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    click.echo(f"Set {len(parsed)} key(s) in {config_path}")


@config_grp.command("unset")
@click.option(
    "--global",
    "is_global",
    is_flag=True,
    default=False,
    help="Remove from ~/.omnigent/config.yaml (user-level) instead of the project config.",
)
@click.argument("keys", nargs=-1, required=True, metavar="KEY...")
def config_unset(is_global: bool, keys: tuple[str, ...]) -> None:
    """Remove one or more Omnigent defaults.

    :param is_global: When ``True``, remove from ``~/.omnigent/config.yaml``;
        when ``False``, from ``.omnigent/config.yaml`` in cwd.
    :param keys: Keys to remove, e.g. ``("server", "model")``.
    """
    validated = _validate_unset_keys(keys)
    if is_global:
        _save_global_config({}, tuple(validated))
        config_path: Path = _effective_global_config_path()
    else:
        _save_local_config({}, tuple(validated))
        config_path = Path.cwd() / _LOCAL_CONFIG_RELPATH
    click.echo(f"Unset {len(validated)} key(s) from {config_path}")


# Node version hint shared by the preflight problem messages and surfaced
# to the user. The Node-based harness CLIs (Claude Code, Codex, Pi) bundle
# a copy of ``undici`` that calls ``worker_threads.markAsUncloneable`` — a
# Node API added in 22.10 that is absent from every 20.x release. On older
# Node it surfaces as the opaque
# ``TypeError: webidl.util.markAsUncloneable is not a function``.
_NODE_MIN_VERSION_HINT = "Node.js 22 LTS or newer (a 22.10+ API is required)"


def _node_version(node_path: str) -> str | None:
    """
    Return the ``node --version`` string (e.g. ``v20.12.2``) or ``None``.

    Used only to make the "too old" warning concrete; a failure to read the
    version is non-fatal — the caller still reports the underlying problem.

    :param node_path: Absolute path to the ``node`` binary, as resolved by
        :func:`shutil.which`.
    :returns: The trimmed version string, or ``None`` if ``node`` could not
        be invoked.
    """
    try:
        result = subprocess.run(
            [node_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _node_dependency_problem() -> str | None:
    """
    Return a one-line problem if Node is missing or too old, else ``None``.

    The Node-based harnesses (``claude-native``, ``codex``, ``pi``) shell
    out to CLIs that bundle ``undici``; that bundle calls
    ``worker_threads.markAsUncloneable`` (added in Node 22.10). We invoke
    ``node`` to probe for the symbol directly rather than parse
    ``node --version``, so the check tracks the actual capability across
    the 22.x/23.x version split and never goes stale against a hardcoded
    floor.

    :returns: A human-readable description suitable for a warning bullet,
        or ``None`` when Node is present and new enough. A flaky/timed-out
        probe also yields ``None`` — setup should not block on it.
    """
    node = shutil.which("node")
    if node is None:
        return (
            "node not found on PATH — the Claude, Codex, and Pi harnesses need "
            f"{_NODE_MIN_VERSION_HINT}."
        )
    # Probe the exact API the bundled undici calls. Exit 0 ⇒ capability
    # present; exit 1 ⇒ too old; we treat any other failure as inconclusive.
    probe = (
        "process.exit("
        "typeof require('node:worker_threads').markAsUncloneable === 'function' ? 0 : 1)"
    )
    try:
        result = subprocess.run(
            [node, "-e", probe],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return None
    version = _node_version(node)
    detected = f" (detected {version})" if version else ""
    return (
        f"Node.js is too old for the bundled harness CLIs{detected} — they need "
        f"{_NODE_MIN_VERSION_HINT}. Symptom if unfixed: "
        "'TypeError: webidl.util.markAsUncloneable is not a function'."
    )


@contextlib.contextmanager
def _isolated_databricks_cfg() -> collections.abc.Generator[None, None, None]:
    """Run Databricks setup against a temp config containing only our three profiles.

    The temp file starts with just the canonical internal-beta profile
    sections (see ``DEFAULT_PROFILES``) seeded from the original when they
    exist, so there is exactly one section per workspace host and
    ``databricks auth token --host X`` never hits the "multiple profiles
    match" ambiguity error.

    The user's real config is never modified while this context is active.
    On normal exit the three sections are merged back into the original.
    On SIGTERM / SIGINT the temp file is removed and the original is left
    exactly as it was.  SIGKILL cannot be caught, but the original is
    always safe because we never touch it.

    Uses ``DATABRICKS_CONFIG_FILE`` so both subprocess CLI calls *and*
    the direct configparser writes in ``omnigent.onboarding.setup``
    (via ``_databrickscfg_path()``) all operate on the temp file. Also
    strips every entry in ``CONFLICTING_ENV_VARS`` for the duration of
    the context so a stale Databricks credential env var (see that list)
    can't shadow ``--host`` inside ``databricks auth token``.
    """
    import configparser
    import signal
    import tempfile

    from omnigent.onboarding.internal_beta import DEFAULT_PROFILES
    from omnigent.onboarding.setup import CONFLICTING_ENV_VARS

    original_cfg = Path.home() / ".databrickscfg"
    saved_env: dict[str, str | None] = {
        "DATABRICKS_CONFIG_FILE": os.environ.get("DATABRICKS_CONFIG_FILE"),
    }
    for var in CONFLICTING_ENV_VARS:
        saved_env[var] = os.environ.pop(var, None)

    def _restore_env() -> None:
        for var, prev in saved_env.items():
            if prev is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prev

    # Temp file contains only the canonical internal-beta profile sections
    # (see DEFAULT_PROFILES), seeded from the original when they already
    # exist. Everything else is excluded so there is exactly one
    # section per workspace host and `databricks auth token --host X`
    # never hits the "multiple profiles match" ambiguity error.
    orig_cfg = configparser.ConfigParser()
    if original_cfg.exists():
        orig_cfg.read(original_cfg)
    cfg = configparser.ConfigParser()
    for spec in DEFAULT_PROFILES:
        if orig_cfg.has_section(spec.name):
            cfg[spec.name] = dict(orig_cfg[spec.name])

    omnigent_dir = Path.home() / ".omnigent"
    omnigent_dir.mkdir(exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix="databrickscfg-setup-",
        dir=omnigent_dir,
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            cfg.write(f)
    except Exception:
        os.unlink(tmp_name)
        raise
    tmp_path = Path(tmp_name)

    os.environ["DATABRICKS_CONFIG_FILE"] = tmp_name

    def _on_signal(signum: int, _frame: types.FrameType | None) -> None:
        tmp_path.unlink(missing_ok=True)
        _restore_env()
        # Restore the original handler before re-raising so signal chaining
        # (e.g. Click's Ctrl-C → Abort) is preserved rather than falling
        # back to SIG_DFL which would kill the process through the OS.
        signal.signal(signum, prev_sigterm if signum == signal.SIGTERM else prev_sigint)
        signal.raise_signal(signum)

    prev_sigterm = signal.signal(signal.SIGTERM, _on_signal)
    prev_sigint = signal.signal(signal.SIGINT, _on_signal)

    write_tmp: Path | None = None
    try:
        yield
        # Merge canonical sections written by setup back into the real cfg.
        tmp_cfg = configparser.ConfigParser()
        tmp_cfg.read(tmp_path)
        orig_cfg = configparser.ConfigParser()
        if original_cfg.exists():
            orig_cfg.read(original_cfg)
        for spec in DEFAULT_PROFILES:
            if tmp_cfg.has_section(spec.name):
                orig_cfg[spec.name] = dict(tmp_cfg[spec.name])
        write_tmp = original_cfg.with_suffix(".tmp")
        with write_tmp.open("w") as f:
            orig_cfg.write(f)
        write_tmp.replace(original_cfg)
        write_tmp = None
    finally:
        tmp_path.unlink(missing_ok=True)
        if write_tmp is not None:
            write_tmp.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)
        _restore_env()


def _run_configure_databricks() -> None:
    """
    Configure coding harnesses to use Databricks Unity AI Gateway.

    Shells out to ``ucode configure`` to authenticate workspaces and set
    up harnesses (Claude SDK, Codex, OpenAI Agents, Pi). After setup,
    Omnigent reads ``~/.ucode/state.json`` to pick per-harness model
    defaults and base URLs.

    :returns: None.
    :raises click.ClickException: If ucode command resolution,
        configuration, or state verification fails.
    """
    ucode_command = find_ucode_command()
    # ucode only configures the model-serving gateway, so it gets the
    # gateway workspace(s) only — not the MCP-only profiles, which are
    # authenticated during profile onboarding and have no ucode role.
    workspace_urls = model_gateway_workspace_urls()
    click.echo("Running `ucode configure --workspaces ...`...")

    result = subprocess.run(
        build_ucode_configure_command(ucode_command, workspace_urls=workspace_urls),
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"`ucode configure` exited with code {result.returncode}; "
            "see the command output above for details."
        )

    click.echo("ucode configuration complete. Omnigent will use state.json for harness setup.")


def _warn_missing_harness_dependencies() -> None:
    """
    Warn about external (non-Python) tools the coding harnesses need.

    Surfaces every missing/outdated dependency up front (when the user
    opens ``configure harnesses``) so a fresh machine learns about all of
    them at once, rather than discovering each at the moment a harness or
    wrapper needs it (Node when a harness CLI runs, tmux when ``omnigent
    claude`` launches). This *warns* rather than aborts on purpose: the
    pure-Python ``openai-agents`` harness runs without either tool, so a
    hard failure would block a valid flow — but ``omnigent claude`` /
    ``codex`` do need both, hence the prominent notice.

    :returns: None. Side effect: writes a yellow warning block to stderr
        via :func:`click.secho` when one or more dependencies are missing.
    """
    problems: list[str] = []
    node_problem = _node_dependency_problem()
    if node_problem is not None:
        problems.append(node_problem)
    if shutil.which("tmux") is None:
        problems.append(
            "tmux not found on PATH — `omnigent claude` and `omnigent codex` launch "
            "the agent through a local tmux terminal and refuse to start without it "
            "(macOS: `brew install tmux`)."
        )
    if not problems:
        return
    click.secho(
        "\n⚠ External tooling needed for some harnesses is missing or outdated:",
        fg="yellow",
        bold=True,
        err=True,
    )
    for problem in problems:
        click.secho(f"  • {problem}", fg="yellow", err=True)
    click.secho(
        "You can still configure credentials — the pure-Python openai-agents harness "
        "runs without these — but install them before `omnigent claude` / "
        "`omnigent codex` or the Pi harness.\n",
        fg="yellow",
        err=True,
    )


def _print_credentials_by_harness() -> None:
    """Print configured model credentials grouped by harness (the ``config list`` view).

    Renders the effective config **merged with ambient detections** (a
    detected env key / CLI login shows as an ordinary credential, with no
    separate "detected vs configured" split) grouped under each harness
    family, with the per-family default marked — via
    :func:`render_provider_listing_by_harness`.

    :returns: None. Side effect: writes the listing to the onboarding
        console.
    """
    from omnigent.onboarding.configure_models import render_provider_listing_by_harness
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import load_providers

    config = effective_config_with_detected(_load_effective_config())
    providers = load_providers(config)
    render_provider_listing_by_harness(config, providers)


def _existing_key_name_for_ref(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    api_key_ref: str,
) -> str | None:
    """Return the name of a ``key`` provider on *family* using *api_key_ref*.

    Two API keys are "the same key" when they read the same secret source
    (the same ``env:`` / ``keychain:`` reference). The add flow uses this to
    update such a key in place rather than writing a second, identical entry —
    so re-adding a key you already have stays idempotent, while a key from a
    genuinely different source gets its own entry (the "keep both" behavior).

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family the key serves, ``"anthropic"`` or
        ``"openai"``.
    :param api_key_ref: The secret reference to match, e.g.
        ``"env:ANTHROPIC_API_KEY"`` or ``"keychain:anthropic"``.
    :returns: The provider name whose *family* block references the same
        secret, e.g. ``"anthropic"``, or ``None`` when no such key exists.
    """
    from omnigent.onboarding.provider_config import KEY_KIND, load_providers

    for name, entry in load_providers(config).items():
        if entry.kind != KEY_KIND:
            continue
        fam = entry.families.get(family)
        if fam is not None and fam.api_key_ref == api_key_ref:
            return name
    return None


def _unique_provider_name(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    candidate: str,
) -> str:
    """Return *candidate*, suffixed numerically until it's a free provider name.

    Provider names key the ``providers:`` mapping, so a colliding name would
    overwrite an existing entry on deep-merge. When the add flow keeps a
    second credential (an API key from a new source for a vendor that already
    has one), this derives a fresh name — ``anthropic`` → ``anthropic-2`` →
    ``anthropic-3`` — so both coexist.

    :param config: The parsed global config mapping (``providers:`` block).
    :param candidate: The preferred name, e.g. ``"anthropic"``.
    :returns: *candidate* if unused, else the first free ``<candidate>-<n>``
        (``n`` starting at 2), e.g. ``"anthropic-2"``.
    """
    from omnigent.onboarding.provider_config import load_providers

    existing = set(load_providers(config))
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}-{n}" in existing:
        n += 1
    return f"{candidate}-{n}"


def _resolve_key_provider_name(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    candidate: str,
    api_key_ref: str,
) -> str:
    """Pick the entry name for an API key being added — update vs keep-both.

    Realizes the "allow multiple API keys, keep both if source differs"
    behavior: a key whose secret source (*api_key_ref*) matches an existing
    key on *family* reuses that entry's name (an in-place update of the same
    credential); a key from a new source takes a fresh, unique name so it
    coexists with the others.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family the key serves, ``"anthropic"`` or
        ``"openai"``.
    :param candidate: The preferred name (the vendor id for a preset, or the
        user-typed name for "Other provider"), e.g. ``"anthropic"``.
    :param api_key_ref: The key's secret reference, e.g.
        ``"env:ANTHROPIC_API_KEY"`` or ``"keychain:anthropic"``.
    :returns: The existing same-source entry's name (update in place), else a
        unique name derived from *candidate* (keep both), e.g.
        ``"anthropic-2"``.
    """
    same_source = _existing_key_name_for_ref(config, family, api_key_ref)
    if same_source is not None:
        return same_source
    return _unique_provider_name(config, candidate)


def _credential_source_hint(entry: ProviderEntry, family: str) -> str | None:
    """A short, non-secret descriptor of where a key's secret comes from.

    Used to disambiguate two API keys that would otherwise share a label
    (e.g. two "Anthropic API Key" rows): an ``env:`` ref renders as
    ``$VAR``, a ``keychain:`` ref as its stored name, an inline ``$VAR`` as
    itself. Only meaningful for credential kinds that carry an inline family
    block (``key`` / ``gateway`` / ``local``).

    :param entry: The parsed provider entry.
    :param family: The surface whose secret source to describe,
        ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: A display hint such as ``"$ANTHROPIC_API_KEY"`` or
        ``"anthropic-2"``, or ``None`` when the family has no resolvable
        source descriptor.
    """
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        PI_SURFACE,
    )

    raw = entry.families.get(family)
    if raw is None and family == PI_SURFACE:
        # The pi surface carries no family block of its own — pi consumes
        # the credential of whichever family it routes through (anthropic
        # preferred), so describe that family's source instead.
        for fam in (ANTHROPIC_FAMILY, OPENAI_FAMILY):
            raw = entry.families.get(fam)
            if raw is not None:
                break
    if raw is None:
        return None
    if raw.api_key_ref is not None:
        if raw.api_key_ref.startswith("env:"):
            return f"${raw.api_key_ref[len('env:') :]}"
        if raw.api_key_ref.startswith("keychain:"):
            return raw.api_key_ref[len("keychain:") :]
    if raw.api_key is not None and raw.api_key.startswith("$"):
        return raw.api_key
    return None


def _family_key_count(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
) -> int:
    """Count the ``key`` providers serving *family*.

    The ``($VAR)`` disambiguation hint is shown only when more than one API
    key serves a harness — a lone key needs no source qualifier.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family, ``"anthropic"`` or ``"openai"``.
    :returns: The number of ``kind: key`` providers serving *family*.
    """
    from omnigent.onboarding.provider_config import (
        KEY_KIND,
        load_providers,
        provider_families,
    )

    return sum(
        1
        for entry in load_providers(config).values()
        if entry.kind == KEY_KIND and family in provider_families(entry)
    )


def _family_credential_label(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    name: str,
    entry: ProviderEntry,
) -> str:
    """A credential label, qualified with its source when keys would collide.

    Wraps :func:`_credential_label`, appending the ``($VAR)`` source hint for
    a ``key`` provider when more than one API key serves *family* (so two
    "Anthropic API Key" rows read as distinct). Non-key kinds and the
    single-key case render the plain label.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family in context, ``"anthropic"`` /
        ``"openai"``.
    :param name: The provider id keyed under ``providers:``, e.g.
        ``"anthropic-2"``.
    :param entry: The parsed provider entry.
    :returns: A human label, e.g. ``"Anthropic API Key ($ANTHROPIC_API_KEY)"``
        when disambiguation applies, else ``"Anthropic API Key"``.
    """
    from omnigent.onboarding.provider_config import KEY_KIND

    base = _credential_label(name, entry)
    if entry.kind != KEY_KIND or _family_key_count(config, family) <= 1:
        return base
    hint = _credential_source_hint(entry, family)
    return f"{base} ({hint})" if hint else base


def _configure_harness_add(family: str | None = None) -> str | None:
    """Run the interactive ``add a provider`` flow and persist the entry.

    Prompts for the provider kind (key / subscription / gateway /
    databricks), gathers the kind-specific fields, deep-merges the single
    entry under ``providers:`` (an add never rewrites siblings), and makes
    it the default for any family it serves that has **no** default yet
    (so a first provider just works; an existing default is left for the
    user to change by selecting it in the harness tree).

    :param family: When set (``"anthropic"`` / ``"openai"`` / ``"pi"``),
        the add menu is scoped to credentials that can drive that harness —
        the per-harness "Add a provider" path. ``None`` shows the full menu.
    :returns: A confirmation message for the caller to show as a transient
        status. Side effect: writes to ``~/.omnigent/config.yaml`` and,
        for a pasted API key, the secret store.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.ambient import detect_providers
    from omnigent.onboarding.configure_models import (
        AddOption,
        add_menu_options,
        add_menu_options_for_family,
        build_cli_config_provider_entry,
        build_databricks_provider_entry,
        build_gateway_provider_entry,
        build_key_provider_entry,
        build_subscription_provider_entry,
        default_base_url_for_family,
        family_for_key_provider,
        key_provider_endpoint,
        other_key_providers,
        provider_display_name,
    )
    from omnigent.onboarding.interactive import console, prompt_text, select
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        CHAT_WIRE_API,
        CLI_CONFIG_KIND,
        DATABRICKS_KIND,
        OPENAI_FAMILY,
        PI_SURFACE,
        RESPONSES_WIRE_API,
        SUBSCRIPTION_KIND,
        load_providers,
        provider_entry_settings,
        set_default_provider,
    )

    # The ucode agent that backs each harness surface's model serving. When the
    # user adds Databricks from a specific harness page, we configure ucode for
    # ONLY that harness (not all of claude/codex/pi) so ucode touches just the
    # one tool the user is wiring up.
    _FAMILY_UCODE_AGENT = {ANTHROPIC_FAMILY: "claude", OPENAI_FAMILY: "codex", PI_SURFACE: "pi"}

    # A flat, credential-aware menu: the user picks "OpenAI — API key" or
    # "Claude — subscription" directly (rather than a bare kind then
    # provider two-step). Each option carries the resolved kind and, for
    # the common cases, a preset provider/cli. When entered from a specific
    # harness, the menu is scoped to that harness's surface.
    options = add_menu_options_for_family(family) if family is not None else add_menu_options()
    # A custom provider defined by the user's own ~/.codex/config.toml
    # (e.g. isaac's Databricks AI Gateway) that is not currently configured
    # gets its own add option. This is the only way back after Remove —
    # removal dismisses the detection so it stops auto-adopting, and there
    # is nothing to type/paste here (the credential lives in that file).
    cli_config_dets: list[DetectedProvider] = []
    if family in (None, OPENAI_FAMILY):
        configured_names = set(load_providers(_load_global_config()))
        cli_config_dets = [
            d
            for d in detect_providers()
            if d.kind == CLI_CONFIG_KIND and d.name not in configured_names
        ]
    # Base options first, then one row per detected config provider — the
    # selection index maps back into cli_config_dets below.
    base_option_count = len(options)
    options = options + [
        AddOption(
            label=f"\N{GEAR}\N{VARIATION SELECTOR-16} {d.display_name or d.name} — "
            "from your Codex config",
            description=(
                f"Use the {str(d.model_provider)!r} provider your ~/.codex/config.toml "
                "defines and authenticates."
            ),
            kind=CLI_CONFIG_KIND,
        )
        for d in cli_config_dets
    ]
    choice = select(
        "What do you want to add?",
        [o.label for o in options],
        descriptions=[o.description for o in options],
        clear_on_exit=True,
    )
    if choice < 0:  # Esc — abort the add
        return None
    chosen = options[choice]
    kind = chosen.kind

    name: str
    # Any (not object): this entry is handed to provider_entry_settings /
    # set_default_provider, which type their config mappings as object;
    # _ConfigValue would trip dict invariance against those. Matches the
    # cli.py yaml-boundary convention.
    entry: dict[str, Any]  # type: ignore[explicit-any]

    if kind == CLI_CONFIG_KIND:
        # One detected-config row was appended per cli_config_dets entry, in
        # order, after the base options — map the selection back to its
        # detection. Nothing to prompt for: the provider definition AND its
        # credential live in ~/.codex/config.toml; the entry only pins it.
        det = cli_config_dets[choice - base_option_count]
        if det.model_provider is None:  # always set on cli-config detections
            raise click.ClickException("internal: cli-config detection missing model_provider")
        name = det.name
        entry = build_cli_config_provider_entry("codex", det.model_provider, det.display_name)
        # Re-adding is the user saying "I want this auto-detected credential
        # after all" — drop any standing dismissal so it behaves like an
        # ordinary detection again (e.g. re-adopts after a config self-heal).
        _clear_detection_dismissal(name)

    elif kind == "key":
        if chosen.provider is not None:
            provider = chosen.provider  # preset by the flat option (OpenAI/Anthropic/OpenRouter)
            # Preset: the preferred name is the provider id — but the final name
            # is resolved from the key's source below (update in place vs keep
            # both), so a second key for the same vendor doesn't overwrite the
            # first.
            candidate = provider
        else:
            # "Other provider — API key": pick from the remaining catalog,
            # shown by friendly display name. This is the one key case where a
            # custom name is useful (e.g. two configs for the same vendor), so
            # it's the only non-gateway path that still prompts for a name.
            others = other_key_providers()
            _other_choice = select(
                "Which provider?",
                [provider_display_name(p) for p in others],
                clear_on_exit=True,
            )
            if _other_choice < 0:  # Esc — abort the add
                return None
            provider = others[_other_choice]
            candidate = prompt_text("Name for this provider", default=provider)
        disp = provider_display_name(provider)
        family = family_for_key_provider(provider)
        # The entry name is resolved from the key's source (not just the
        # candidate): a key whose source matches an existing one updates it in
        # place, while a key from a new source takes a fresh name so both
        # coexist ("allow multiple API keys"). See _resolve_key_provider_name.
        config_now = _load_global_config()
        # Offer to reuse a detected env var for this provider rather than
        # forcing the user to re-paste a key they already have in the env.
        detected = {d.name: d for d in detect_providers()}
        api_key_ref: str
        if (
            provider in detected
            and detected[provider].kind == "key"
            and click.confirm(
                f"Detected {detected[provider].source} in the environment — use it?",
                default=True,
            )
        ):
            env_var = detected[provider].source.lstrip("$")  # e.g. "ANTHROPIC_API_KEY"
            api_key_ref = f"env:{env_var}"
            name = _resolve_key_provider_name(config_now, family, candidate, api_key_ref)
        else:
            # A pasted key is stored at keychain:<name>; resolve the name first
            # (an existing key in this same keychain slot is replaced in place,
            # otherwise we pick a free name) so we store under and reference the
            # final name.
            name = _resolve_key_provider_name(
                config_now, family, candidate, f"keychain:{candidate}"
            )
            pasted = prompt_text(f"{disp} API key", hide_input=True)
            secret_store.store_secret(name, pasted)
            api_key_ref = f"keychain:{name}"

        # Default model — free-form text entry. The bundled catalog lags new
        # releases (e.g. a brand-new claude-sonnet-4-6 won't be listed yet), so
        # a fixed picker would block the user from a model they can actually
        # use. Pre-fill the canonical default and let the user type ANY model
        # id. Blank → the default (or no pin when unknown). Always persisting
        # a pin keeps a later re-add from silently dropping ``models.default``.
        from omnigent.onboarding.providers import default_chat_model

        catalog_default = default_chat_model(provider)
        # default=catalog_default (str | None): a known provider pre-fills its
        # default (blank-enter accepts it); an unknown provider has no default,
        # so the user types a model id. ``.strip() or None`` keeps an
        # all-whitespace entry from becoming a bogus pin.
        typed = prompt_text("Default model", default=catalog_default)
        default_model = typed.strip() or None

        # A third-party OpenAI-compatible vendor (OpenRouter, Groq, …) is
        # reached at its OWN base_url and speaks Chat Completions; openai /
        # anthropic use the canonical family endpoint (and openai keeps the
        # Responses default). Using the family default for a vendor sent its
        # traffic to api.openai.com — the reason an OpenRouter key failed.
        endpoint = key_provider_endpoint(provider)
        if endpoint is not None:
            base_url = endpoint.base_url
            key_wire_api: str | None = endpoint.wire_api
        else:
            base_url = default_base_url_for_family(family)
            key_wire_api = None
        entry = build_key_provider_entry(
            family=family,
            base_url=base_url,
            api_key_ref=api_key_ref,
            default_model=default_model,
            wire_api=key_wire_api,
        )

    elif kind == "subscription":
        cli_name = chosen.cli  # preset by the flat option (claude / codex)
        if cli_name is None:
            raise click.ClickException("internal: subscription option missing a cli login")
        from omnigent.onboarding.harness_install import harness_install_spec, harness_login

        login_family = {agent: fam for fam, agent in _FAMILY_UCODE_AGENT.items()}.get(cli_name)
        if login_family is None:
            raise click.ClickException(f"internal: no login family for cli {cli_name!r}")
        spec = harness_install_spec(login_family)
        disp = spec.display if spec is not None else cli_name
        # A harness has at most ONE subscription — the CLI's own login. If one
        # is already configured for this CLI (under any name, including an
        # ambient login adopted as e.g. ``claude``), adding another just
        # duplicates it — the ``claude`` + ``claude-subscription`` bug. Offer to
        # replace the existing one; declining aborts before we touch the login.
        existing_subs = [
            n
            for n, e in load_providers(_load_global_config()).items()
            if e.kind == SUBSCRIPTION_KIND and e.cli == cli_name
        ]
        if existing_subs:
            brand = _CLI_LOGIN_BRAND.get(cli_name, cli_name)
            replace = select(
                f"A {brand} subscription is already configured. Replace it?",
                ["Replace it", "Keep the current one"],
                default=0,
                clear_on_exit=True,
            )
            if replace != 0:  # "Keep the current one" or Esc — abort the add
                return None
        # Configure is the single place to sign in: drive the harness's own
        # login (a no-op if already logged in). Only record the subscription
        # once the CLI is actually authenticated — otherwise we'd persist a
        # phantom subscription that strands the user at the harness's own login
        # screen at run time (the exact bug this whole flow fixes).
        console.print(f"  [dim]Signing in to {disp} (its login will open)…[/dim]")
        if not harness_login(login_family):
            return f"✗ {disp} login not completed — subscription not added"
        # Login succeeded — drop the existing subscription(s) for this CLI so the
        # canonical entry is the only one left (clearing the old default lets the
        # new entry re-claim the family default below). Done AFTER login so a
        # failed login leaves the existing subscription intact.
        if existing_subs:
            block = _load_global_config().get("providers")
            if isinstance(block, dict):
                remaining = {k: v for k, v in block.items() if k not in existing_subs}
                _save_global_config({"providers": remaining})  # wholesale replace
        # Subscription name is derived from the CLI login — no prompt.
        name = f"{cli_name}-subscription"
        entry = build_subscription_provider_entry(cli_name)

    elif kind == "gateway":
        name = prompt_text("Name for this gateway", default="gateway")
        base_url = prompt_text("Gateway base_url (OpenAI/Anthropic-compatible)")
        pasted = prompt_text("Gateway API key", hide_input=True)
        secret_store.store_secret(name, pasted)
        # Which harness surfaces — one clear pick instead of two y/n prompts.
        # (These are *harness* surfaces: Codex/OpenAI → codex + openai-agents;
        # Claude/Anthropic → claude-sdk + native-claude.)
        surface_choice = select(
            "Which harnesses can this gateway drive?",
            [
                "Both Claude and Codex",
                "Codex / OpenAI only (codex, openai-agents)",
                "Claude only (claude-sdk, native-claude)",
            ],
            default=0,
            clear_on_exit=True,
        )
        if surface_choice < 0:  # Esc — abort the add
            return None
        families = (
            [OPENAI_FAMILY, ANTHROPIC_FAMILY]
            if surface_choice == 0
            else [OPENAI_FAMILY]
            if surface_choice == 1
            else [ANTHROPIC_FAMILY]
        )
        # Wire protocol for the OpenAI surface: OpenAI / LiteLLM speak the
        # Responses API; OpenRouter and many OSS-model gateways are
        # Chat-Completions-only. Picking wrong makes every turn fail (the
        # exact "OpenRouter doesn't work but LiteLLM does" symptom), so ask —
        # defaulting to Chat when the URL looks like OpenRouter.
        wire_api: str | None = None
        if OPENAI_FAMILY in families:
            wire_choice = select(
                "OpenAI wire protocol for this gateway?",
                [
                    "Responses API (OpenAI, LiteLLM)",
                    "Chat Completions (OpenRouter, most OSS-model gateways)",
                ],
                default=1 if "openrouter" in base_url.lower() else 0,
                clear_on_exit=True,
            )
            if wire_choice < 0:  # Esc — abort the add
                return None
            wire_api = RESPONSES_WIRE_API if wire_choice == 0 else CHAT_WIRE_API
        # Default model per served surface. A gateway has NO catalog default,
        # so without a pin routing would fall back to a vendor model the
        # gateway can't serve. The OpenAI surface pre-fills a broadly-served
        # OSS default (moonshotai/kimi-k2.6, via the openrouter pin); the
        # user can type any gateway model id.
        from omnigent.onboarding.providers import default_chat_model

        models: dict[str, str] = {}
        if OPENAI_FAMILY in families:
            models[OPENAI_FAMILY] = prompt_text(
                "Default model for the Codex / OpenAI surface",
                default=default_chat_model("openrouter"),
            ).strip()
        if ANTHROPIC_FAMILY in families:
            models[ANTHROPIC_FAMILY] = prompt_text(
                "Default model for the Claude surface (the gateway's Claude model id)"
            ).strip()
        entry = build_gateway_provider_entry(
            base_url=base_url,
            api_key_ref=f"keychain:{name}",
            families=families,
            wire_api=wire_api,
            models=models,
        )

    else:  # databricks
        # Gate on the `databricks` extra: a `kind: databricks` provider mints
        # workspace OAuth tokens via databricks-sdk at runtime
        # (omnigent/runtime/credentials/databricks.py), and the SDK is no
        # longer a default dependency. Abort before any side effect (the
        # `databricks auth login` browser flow, `ucode configure`) so the
        # user isn't signed into a workspace that routing then can't use.
        from omnigent.onboarding.databricks_config import (
            DATABRICKS_EXTRA_INSTALL_HINT,
            databricks_sdk_installed,
        )

        if not databricks_sdk_installed():
            from rich.markup import escape as _rich_escape

            # The status renders through Text.from_markup, where the literal
            # `[databricks]` in the install command would parse as a tag.
            return (
                "✗ Databricks routing needs the databricks extra — "
                f"{_rich_escape(DATABRICKS_EXTRA_INSTALL_HINT)}"
            )

        # The intro + URL prompt render inline, exactly like every other add
        # flow (the add-menu picker already erased its own frame on exit via
        # `clear_on_exit`) — entering the Databricks option should NOT blank the
        # whole screen. The one clear we keep is *after* the subprocess (below):
        # `databricks auth login` + `ucode configure` print a lot, and the
        # in-place menu redraw we return to can only erase its own frame, so we
        # wipe that leftover output once the login finishes.
        # Ask only for the workspace URL — never a profile name. The flow
        # below authenticates that one workspace and runs `ucode configure`
        # against it, scoped to the harness the user drilled into. This is
        # the one place Omnigent triggers a Databricks CLI / ucode login;
        # it never happens on a bare `run`, so a user who only wants their
        # own provider is never routed through Databricks unexpectedly.
        from omnigent.onboarding.configure_models import family_label
        from omnigent.onboarding.interactive import clear_screen
        from omnigent.onboarding.setup import login_databricks_workspace
        from omnigent.onboarding.ucode_setup import (
            configure_ucode_for_workspace,
            ucode_workspace_exists,
        )

        _routed = f"{family_label(family)}'s" if family is not None else "your harnesses'"
        console.print(
            f"  [dim]Routes {_routed} model calls through this workspace's "
            "Databricks Unity AI Gateway (via ucode), so usage is governed and "
            "billed there. This signs you into the workspace and runs "
            "`ucode configure` for it.[/dim]"
        )
        workspace_url = prompt_text(
            "Databricks workspace URL (e.g. https://example.cloud.databricks.com)"
        ).strip()
        if not workspace_url:  # blank — abort the add
            return None
        if not workspace_url.startswith(("http://", "https://")):
            workspace_url = f"https://{workspace_url}"
        workspace_url = workspace_url.rstrip("/")

        # 1. Authenticate the workspace (returns the ~/.databrickscfg profile
        #    name) and 2. run `ucode configure` against it for model serving —
        #    scoped to the harness the user drilled into (or both when added
        #    from the un-scoped menu), so ucode configures only what's needed.
        if family is not None:
            ucode_agents = [_FAMILY_UCODE_AGENT[family]]
        else:
            ucode_agents = sorted(_FAMILY_UCODE_AGENT.values())
        profile = login_databricks_workspace(workspace_url, console=console)
        configure_ucode_for_workspace(workspace_url, agents=ucode_agents)
        # Fail loud if ucode didn't actually record state for the workspace —
        # otherwise routing would silently fall back and confuse the user.
        if not ucode_workspace_exists(workspace_url):
            raise click.ClickException(
                f"`ucode configure` finished but recorded no state for {workspace_url}. "
                "Re-run and check the ucode output above."
            )
        # Wipe the verbose login + ucode output so the menu we return to (with a
        # "✓ Added databricks" status) renders on a clean screen.
        clear_screen()
        # Databricks name is fixed — no prompt. The provider keys on the
        # profile; runtime resolves profile → workspace URL → ucode state.
        name = "databricks"
        entry = build_databricks_provider_entry(profile)

    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.provider_config import (
        provider_families,
        surface_default_provider,
    )

    # Persist the entry (deep-merge — doesn't disturb sibling entries).
    _save_global_config(
        provider_entry_settings(name, entry, make_default=False),
        deep_merge_keys=("providers",),
    )
    # Become the default for any surface it serves that has NO default yet,
    # so a first provider "just works". An existing default is left alone —
    # the user changes defaults by selecting a provider in the harness tree
    # (per-surface, so a shared provider can default one harness, not both).
    # The pi surface checks its *effective* default: a family default already
    # drives pi via the fallback, so claiming the explicit pi scope then
    # would silently re-route pi away from it.
    parsed = load_providers({"providers": {name: entry}})[name]
    # Databricks routing is configured in ucode PER HARNESS (we only ran
    # `ucode configure` for the surface the user drilled into), so it must only
    # become the default for THAT surface — defaulting the other harnesses too
    # would route them through a workspace ucode never configured for them.
    # Other kinds (a gateway serving both families with one base_url + key)
    # still default every surface they serve.
    if entry["kind"] == DATABRICKS_KIND and family is not None:
        default_families = [family]
    else:
        default_families = sorted(provider_families(parsed))
    became_default: list[str] = []
    for fam in default_families:
        cfg = _load_global_config()
        if surface_default_provider(cfg, fam) is not None:
            continue
        block = cfg.get("providers")
        if isinstance(block, dict):
            _save_global_config({"providers": set_default_provider(block, name, fam)})
            became_default.append(fam)
    if became_default:
        labels = " · ".join(family_label(f) for f in became_default)
        return f"✓ Added {name} — default for {labels}"
    return f"✓ Added {name}"


def _adopt_detected_providers() -> list[str]:
    """Persist ambient-detected providers into the config, returning new names.

    Opening ``configure harnesses`` adopts any detected credential (env key,
    CLI login, local Ollama) not already in ``providers:`` as a real,
    editable entry — so the tree shows one uniform provider list with no
    "detected vs configured" split. Writes the merged view (explicit +
    detected, with detected auto-defaulting per family) wholesale, and only
    when there is something new to adopt (idempotent on re-open).

    :returns: The names adopted this call, e.g. ``["anthropic", "codex"]``;
        empty when every detection is already configured.
    """
    from omnigent.onboarding.detected import (
        effective_config_with_detected,
        providers_to_adopt,
    )

    config = _load_global_config()
    to_adopt = providers_to_adopt(config)
    if not to_adopt:
        return []
    merged = effective_config_with_detected(config)
    _save_global_config({"providers": merged["providers"]})  # wholesale replace
    return list(to_adopt)


def _promote_global_auth_to_provider() -> str | None:
    """Backfill a databricks providers entry from an existing global ``auth:`` block.

    Older ``omnigent setup`` runs configured Databricks only via the top-level
    ``auth: {type: databricks}`` block — which ``configure harnesses`` does not
    read — so the readout showed no Databricks provider (and an ambient CLI
    login as the default) even though routing used Databricks. This promotes
    that block into a first-class ``kind: databricks`` providers entry the next
    time ``configure harnesses`` opens, so existing configs self-heal without
    re-running ``omnigent setup``.

    Becomes the default only for families with no existing **provider** default —
    mirroring routing precedence (explicit provider default > ``auth:`` block),
    so an explicitly-chosen default is left untouched while a config that only
    ever had the ``auth:`` block gets Databricks as its default (matching what
    routing already does at runtime). Must run BEFORE
    :func:`_adopt_detected_providers` so Databricks claims the default ahead of
    an ambient CLI login (``auth:`` outranks ambient detection in routing too).

    :returns: ``"databricks"`` if a provider was backfilled, else ``None`` (no
        databricks ``auth:`` block, or a databricks provider already exists).
    """
    from omnigent.onboarding.configure_models import build_databricks_provider_entry
    from omnigent.onboarding.provider_config import (
        load_providers,
        provider_entry_settings,
        provider_families,
        set_default_provider,
        surface_default_provider,
    )

    config = _load_global_config()
    auth = config.get("auth")
    if not isinstance(auth, dict) or auth.get("type") != "databricks":
        return None
    profile = auth.get("profile")
    if not isinstance(profile, str) or not profile:
        return None
    name = "databricks"
    if name in load_providers(config):
        return None  # already a first-class provider — nothing to backfill

    entry = build_databricks_provider_entry(profile)
    _save_global_config(
        provider_entry_settings(name, entry, make_default=False),
        deep_merge_keys=("providers",),
    )
    parsed = load_providers({"providers": {name: entry}})[name]
    for fam in sorted(provider_families(parsed)):
        cfg = _load_global_config()
        # Effective check (matters for the pi surface): a default that
        # already drives the surface — explicitly or via pi's fallback —
        # outranks the legacy auth: block, exactly like routing does.
        if surface_default_provider(cfg, fam) is not None:
            continue  # respect an existing provider default (it outranks auth:)
        block = cfg.get("providers")
        if isinstance(block, dict):
            _save_global_config({"providers": set_default_provider(block, name, fam)})
    return name


def _compact_credential_label(det: DetectedProvider) -> str:
    """A short, brand-qualified label for an auto-configured credential.

    Unlike :func:`omnigent.onboarding.configure_models.credential_label`
    (which renders every CLI login as a bare ``"Subscription"`` because a
    harness only ever has one), this names the *brand* behind a login —
    ``"Claude Subscription"`` / ``"ChatGPT Subscription"`` — so a single
    comma-joined callout listing several credentials at once stays unambiguous
    without a per-line source. API keys and local endpoints reuse the shared
    ``credential_label`` (``"Anthropic API Key"``, ``"Ollama"``).

    :param det: A credential found by
        :func:`omnigent.onboarding.ambient.detect_providers`.
    :returns: A short human label, e.g. ``"Anthropic API Key"``,
        ``"Claude Subscription"``, or ``"ChatGPT Subscription"``.
    """
    from omnigent.onboarding.ambient import SUBSCRIPTION_KIND
    from omnigent.onboarding.configure_models import credential_label

    if det.kind == SUBSCRIPTION_KIND:
        # Fallback to the raw CLI name is unreachable for today's detections
        # (see _CLI_LOGIN_BRAND) but keeps an added CLI readable, not crashing.
        brand = _CLI_LOGIN_BRAND.get(det.name, det.name)
        return f"{brand} Subscription"
    # A cli-config detection carries the provider's own display name
    # ("Databricks AI Gateway"); other kinds ignore the keyword.
    return credential_label(det.kind, det.name, display_name=det.display_name)


def _announce_auto_configured_credentials(adopted: list[str]) -> None:
    """Print the "found existing credentials → auto-configured" callout.

    Re-runs ambient detection to recover each adopted credential, then prints a
    single compact, dimmed line naming them inline (e.g. ``Anthropic API Key,
    Claude Subscription, ChatGPT Subscription``) — so a user who never ran an
    explicit setup sees, the first time we auto-configure, exactly which
    credentials omnigent picked up (rather than silently inheriting them).
    Styled ``dim`` rather than the onboarding accent so it reads as a quiet
    notice, not a prominent header.

    :param adopted: Provider names just persisted by
        :func:`_adopt_detected_providers`, e.g. ``["anthropic", "codex"]``.
        A name with no matching live detection is skipped (defensive — the
        adopt set and the detection list come from the same detection pass, so
        in practice every name resolves).
    :returns: None. Side effect: writes the callout to the shared onboarding
        console (stdout). Prints nothing when no adopted name resolves to a
        live detection.
    """
    from omnigent.onboarding.ambient import detect_providers
    from omnigent.onboarding.interactive import console

    detected = {det.name: det for det in detect_providers()}
    labels = [_compact_credential_label(detected[name]) for name in adopted if name in detected]
    if not labels:
        return
    console.print(
        "\n[dim]Found existing credentials on your machine, "
        f"auto-configured for omnigent: {', '.join(labels)}[/dim]"
    )


def _adopt_ambient_credentials(progress: RunnerStartupProgress | None = None) -> list[str]:
    """Self-heal config, adopt ambient credentials, and announce what was added.

    The shared front half of both a bare ``omnigent run``'s first-run path
    (:func:`_resolve_first_run_plan`) and the ``configure harnesses`` picker
    (:func:`_run_configure_harnesses_interactive`): it (1) backfills a legacy
    databricks ``auth:`` block into a real provider, (2) adopts any
    ambient-detected credential (env API key, logged-in ``claude`` / ``codex``
    CLI, local Ollama) not already configured as an ordinary provider entry,
    and (3) prints a callout naming exactly the credentials it just
    auto-configured. Idempotent: a second open adopts nothing, so no callout
    prints.

    The callout is scoped to *machine* credentials — the ambient detections —
    not the databricks ``auth:`` backfill, which promotes an existing config
    block rather than something newly "found on your machine".

    :param progress: Optional spinner handle (from
        :func:`omnigent._runner_startup.runner_startup_progress`) covering the
        detection step — slow on macOS, where Claude detection now shells out to
        ``claude auth status`` to read the Keychain. When supplied, it is
        ``finish()``-ed (the spinner cleared) right before the callout prints,
        so the "Found existing credentials…" line is not clobbered by the
        animating spinner. ``None`` (the ``run`` first-run path) means no
        spinner — behavior is unchanged.
    :returns: The provider names adopted this call, e.g. ``["anthropic"]``;
        empty when every detection was already configured.
    """
    _promote_global_auth_to_provider()
    adopted = _adopt_detected_providers()
    # Clear the search spinner (if any) before printing — the callout writes to
    # stdout while the spinner animates on stderr, and on a shared TTY the two
    # would otherwise overwrite each other.
    if progress is not None:
        progress.finish()
    if adopted:
        _announce_auto_configured_credentials(adopted)
    return adopted


@dataclass(frozen=True)
class _HarnessMenuRow:
    """One selectable row in a harness's provider-management menu (level 2).

    :param label: Display text, e.g. ``"🔑 anthropic   ✓ default"``.
    :param action: The action on Enter — ``"set_default"`` / ``"add"`` /
        ``"remove"`` / ``"back"``.
    :param provider: For ``set_default``, the provider name to default;
        ``None`` for the other actions.
    """

    label: str
    action: str
    provider: str | None = None


def _credential_label(name: str, entry: ProviderEntry) -> str:
    """A friendly, jargon-free label for a configured credential.

    A logged-in CLI reads as ``"Subscription"`` (within a harness there is only
    one, so the plan name adds no information); an API-key provider names the
    vendor and the credential type (``"Anthropic API Key"`` / ``"OpenAI API
    Key"``); Databricks as ``"Databricks (<profile>)"``; a gateway / local
    endpoint as its display name — so menus and summaries avoid raw provider
    ids and the word "provider".

    :param name: The provider id keyed under ``providers:``, e.g. ``"openai"``.
    :param entry: The parsed provider entry.
    :returns: A human label, e.g. ``"Anthropic API Key"`` or ``"Databricks (oss)"``.
    """
    from omnigent.onboarding.configure_models import credential_label

    return credential_label(
        entry.kind, name, profile=entry.profile, display_name=entry.display_name
    )


def _harness_summary_lines(config: dict[str, Any], family: str) -> list[str]:  # type: ignore[explicit-any]
    """The styled sub-line(s) shown under a harness on the level-1 overview.

    Returns a prominent default line — a bold-green ``✓`` + the default
    credential's label, with the model dimmed — and, when there are other
    credentials, a dim ``+N more`` line (the full list is one keystroke away on
    level 2). Mirrors how ``gh`` / ``gcloud`` summaries surface the active
    item: highlight it, don't enumerate the rest. The returned strings carry
    Rich markup; :func:`_render_menu` indents them without re-styling.

    :param config: The parsed config mapping (``providers:`` block).
    :param family: The harness surface, ``"anthropic"``, ``"openai"``, or
        ``"pi"``.
    :returns: One or two markup sub-lines, e.g. ``["[bold green]✓ Anthropic API
        Key[/][dim]  ·  claude-opus-4-8[/]", "[dim]+1 more[/]"]``, or
        ``["[dim]no credential yet — open to add one[/]"]``.
    """
    from omnigent.onboarding.provider_config import (
        load_providers,
        provider_families,
        surface_default_model,
        surface_default_provider,
    )

    serving = [
        (name, entry)
        for name, entry in load_providers(config).items()
        if family in provider_families(entry)
    ]
    if not serving:
        return ["[dim]no credential yet — open to add one[/]"]
    # The surface's *effective* default: for the family surfaces this is the
    # explicit per-family default; for pi it is what the pi harness would
    # actually route through (explicit pi scope, else the fallback).
    default = surface_default_provider(config, family)
    default_label: str | None = None
    default_model: str | None = None
    others = 0
    for name, entry in serving:
        if default is not None and name == default.name:
            default_label = _family_credential_label(config, family, name, entry)
            default_model = surface_default_model(entry, family)
        else:
            others += 1
    if default_label is None:
        return ["[dim]no default set — open to choose one[/]"]
    default_line = f"[bold green]✓ {default_label}[/]" + (
        f"[dim]  ·  {default_model}[/]" if default_model else ""
    )
    lines = [default_line]
    if others:
        lines.append(f"[dim]+{others} more[/]")
    return lines


def _harness_credential_rows(config: dict[str, Any], family: str) -> list[_HarnessMenuRow]:  # type: ignore[explicit-any]
    """Build the level-2 rows: each credential serving *family*, then ``+ Add``.

    Each credential row drills into level 3 (make default / remove). The
    current default is marked with a green ✓. ``+ Add a credential`` runs the
    add flow; ``← Back`` returns to the harness picker (as do Esc / ``q``).

    :param config: The parsed config mapping (``providers:`` block).
    :param family: The harness surface being managed.
    :returns: The ordered, all-selectable rows.
    """
    from omnigent.onboarding.configure_models import kind_glyph
    from omnigent.onboarding.provider_config import (
        load_providers,
        provider_families,
        surface_default_provider,
    )

    serving = [
        (name, entry)
        for name, entry in load_providers(config).items()
        if family in provider_families(entry)
    ]
    # The surface's effective default (for pi: explicit scope, else fallback)
    # so the ✓ always marks the credential the harness would actually use.
    default = surface_default_provider(config, family)
    rows: list[_HarnessMenuRow] = []
    for name, entry in serving:
        glyph = kind_glyph(entry.kind)
        cred = _family_credential_label(config, family, name, entry)
        # The current default renders bold-green with a ✓ so it stands out in
        # the list; the rest are plain. Provider names are markup-safe in
        # practice (same assumption select() already makes for every label).
        if default is not None and name == default.name:
            label = f"[bold green]{glyph} {cred}  ✓ default[/]"
        else:
            label = f"{glyph} {cred}"
        rows.append(_HarnessMenuRow(label, action="credential", provider=name))
    rows.append(_HarnessMenuRow("+ Add a credential", action="add"))
    rows.append(_HarnessMenuRow("← Back", action="back"))
    return rows


def _prompt_install_harness(family: str) -> bool:
    """Offer to install an uninstalled harness CLI; return whether to proceed.

    Shown when the user drills into a harness whose CLI isn't on PATH. Offers
    three choices: install it now (``npm install -g …``), go back, or print the
    command to run manually.

    :param family: The harness surface being configured (``"anthropic"`` /
        ``"openai"`` / ``"pi"``).
    :returns: ``True`` only when the CLI is installed afterward (user chose
        install and it succeeded), so the caller continues to credential
        configuration; ``False`` when the user declines, asks to run it
        themselves, the install fails, or they Esc — the caller returns to the
        harness picker.
    """
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.harness_install import (
        harness_install_command,
        install_harness_cli,
    )
    from omnigent.onboarding.interactive import console, select

    label = family_label(family)
    cmd = " ".join(harness_install_command(family))
    choice = select(
        f"{label}'s CLI isn't installed. Install it now?",
        [
            f"Yes — install ({cmd})",
            "No — back to harnesses",
            "I'll run it myself (show the command)",
        ],
        descriptions=[
            f"Runs `{cmd}` (needs npm), then continues to credential setup.",
            "Return to the harness picker without installing.",
            "Print the command so you can install it yourself, then return.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Installing {label} — running `{cmd}`…[/dim]")
        if install_harness_cli(family):
            console.print(f"  [green]✓ {label} installed[/green]")
            return True
        console.print(
            f"  [red]Install failed.[/red] Run it manually, then re-open: [bold]{cmd}[/bold]"
        )
        return False
    if choice == 2:  # run it yourself
        console.print(f"  Install {label} with:\n    [bold]{cmd}[/bold]")
    return False


def _manage_harness_providers(family: str) -> None:
    """Run the level-2 loop for one harness: pick a credential or add one.

    Selecting a credential opens level 3 (make default / remove); ``+ Add``
    runs the add flow. Esc (TTY) / ``q`` (fallback) returns to the harness
    picker. The menu re-renders (cleared in place) after each action so the
    session stays on one tidy screen.

    :param family: The harness family being managed.
    :returns: None.
    """
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.harness_install import harness_cli_installed
    from omnigent.onboarding.interactive import select

    # If the harness CLI isn't installed, offer to install it before showing
    # the credential menu. Declining (or copy-the-command) returns to the
    # harness picker — there's nothing to configure for a harness you can't run.
    if not harness_cli_installed(family) and not _prompt_install_harness(family):
        return

    # Carry the prior action's confirmation as a transient status line so the
    # menu shows only the latest result — not an accumulating stack of "✓ …".
    status: str | None = None
    while True:
        rows = _harness_credential_rows(_load_global_config(), family)
        idx = select(
            f"{family_label(family)} — select or add a credential",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:  # Esc / q — back to the harness picker
            return
        row = rows[idx]
        if row.action == "back":
            return
        if row.action == "add":
            status = _configure_harness_add(family=family)
        elif row.action == "credential" and row.provider is not None:
            status = _manage_credential(row.provider, family)


def _manage_cursor_harness() -> None:
    """Run the level-2 loop for Cursor: manage its ``CURSOR_API_KEY``.

    Cursor runs via the ``cursor-sdk`` package and authenticates against
    Cursor's own backend with a ``CURSOR_API_KEY`` — the SDK requires one (a
    ``cursor-agent login`` does not apply, and cursor has no provider/gateway
    family). So this manages exactly that credential: set / replace / remove an
    API key stored in the omnigent secret store, mirroring how the other
    harnesses persist their api keys (the secret in the store, a
    ``keychain:``/``env:`` reference in ``~/.omnigent/config.yaml``).

    :returns: None. Side effects: may write the ``cursor:`` block of
        ``~/.omnigent/config.yaml`` and the secret store.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.cursor_auth import cursor_api_key_configured, cursor_api_key_ref
    from omnigent.onboarding.interactive import select

    status: str | None = None
    while True:
        config = _load_global_config()
        key_set = cursor_api_key_configured(config)

        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow(
                "Replace API key (CURSOR_API_KEY)" if key_set else "Set API key (CURSOR_API_KEY)",
                action="set_key",
            )
        ]
        if key_set:
            rows.append(_HarnessMenuRow("Remove API key", action="remove_key"))
        rows.append(_HarnessMenuRow("← Back", action="back"))

        header = "Cursor — API key configured" if key_set else "Cursor — no API key yet"
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "set_key":
            status = _set_cursor_api_key()
        elif action == "remove_key":
            ref = cursor_api_key_ref(config)
            # Only a keychain-stored secret is ours to delete; an ``env:`` ref
            # points at the user's own environment, so just drop the config.
            if ref is not None and ref.startswith("keychain:"):
                secret_store.delete_secret(ref[len("keychain:") :])
            _save_global_config({}, unset_keys=("cursor",))
            status = "✓ Removed Cursor API key"


def _set_cursor_api_key() -> str | None:
    """Prompt for and store a Cursor ``CURSOR_API_KEY``; return a status line.

    Offers an existing ``CURSOR_API_KEY`` from the environment first (recorded
    as an ``env:`` reference, so the secret never enters the config or the
    secret store), else reads the key with a hidden prompt and stores it in the
    omnigent secret store under ``keychain:cursor``. The ``crsr_`` prefix is
    validated with a soft warning so a wrong paste is caught without
    hard-blocking a future key format. The key value is never echoed.

    :returns: A confirmation string for the menu's transient status, or
        ``None`` when the user aborted (empty input / declined the warning).
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.cursor_auth import (
        CURSOR_SECRET_NAME,
        cursor_api_key_settings,
        looks_like_cursor_api_key,
    )
    from omnigent.onboarding.interactive import prompt_text

    detected = os.environ.get("CURSOR_API_KEY")
    if detected and click.confirm(
        "Detected CURSOR_API_KEY in the environment — use it?", default=True
    ):
        if not looks_like_cursor_api_key(detected) and not click.confirm(
            "$CURSOR_API_KEY doesn't start with 'crsr_'. Use it anyway?", default=False
        ):
            return None
        _save_global_config(cursor_api_key_settings("env:CURSOR_API_KEY"))
        return "✓ Cursor API key set (from $CURSOR_API_KEY)"

    pasted = prompt_text("Cursor API key (CURSOR_API_KEY)", hide_input=True).strip()
    if not pasted:
        return None
    if not looks_like_cursor_api_key(pasted) and not click.confirm(
        "That doesn't start with 'crsr_'. Store it anyway?", default=False
    ):
        return None
    secret_store.store_secret(CURSOR_SECRET_NAME, pasted)
    _save_global_config(cursor_api_key_settings(f"keychain:{CURSOR_SECRET_NAME}"))
    return "✓ Cursor API key stored"


def _prompt_install_antigravity() -> str | None:
    """Offer to install the missing ``antigravity`` extra; return a status line.

    Shown atop the Antigravity drill-in when the ``google-antigravity`` SDK is absent.
    Mirrors :func:`_prompt_install_harness` — a three-choice ``select`` (install now /
    set key anyway / print command) — but does NOT gate key management on the SDK:
    unlike pi (which can't be configured without its CLI), the ``antigravity:`` key is
    storable independently, so declining just falls through to the key menu. The
    install carries no index URL (see :func:`antigravity_install_command`); on failure
    it prints the command to run by hand.

    :returns: A status string for the drill-in's transient status (install result or
        printed-command note), or ``None`` on set-key-anyway / Esc.
    """
    from rich.markup import escape as _rich_escape

    from omnigent.onboarding.antigravity_auth import (
        ANTIGRAVITY_EXTRA_INSTALL_COMMAND,
        install_antigravity_sdk,
    )
    from omnigent.onboarding.interactive import console, select

    cmd = ANTIGRAVITY_EXTRA_INSTALL_COMMAND
    # ``select`` renders through Rich markup, so escape the literal ``[antigravity]``.
    cmd_markup = _rich_escape(cmd)
    choice = select(
        "Antigravity's SDK (google-antigravity) isn't installed. Install it now?",
        [
            f"Install it now ({cmd_markup})",
            "Set the Gemini key anyway",
            "I'll run it myself (show the command)",
        ],
        descriptions=[
            f"Runs `{cmd_markup}` (uses uv when available), then continues.",
            "Skip the install — store the key now; the SDK can be added later.",
            "Print the command so you can install it yourself, then continue.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Installing the antigravity extra — running `{cmd_markup}`…[/dim]")
        if install_antigravity_sdk():
            console.print("  [green]✓ google-antigravity installed[/green]")
            return "✓ google-antigravity installed"
        console.print(f"  [red]Install failed.[/red] Run it manually: [bold]{cmd_markup}[/bold]")
        return "✗ Install failed — set the key anyway, or install by hand"
    if choice == 2:
        console.print(f"  Install the antigravity extra with:\n    [bold]{cmd_markup}[/bold]")
        return None
    # choice == 1 (set key anyway) or Esc: fall through to the key menu silently.
    return None


def _manage_antigravity_harness() -> None:
    """Run the level-2 loop for Antigravity: set / replace / remove its Gemini key.

    Antigravity is Gemini-native (no provider family), so this manages just its
    API key — stored in the secret store, referenced from the ``antigravity:``
    config block — mirroring how the other harnesses persist api keys.

    When the optional ``google-antigravity`` SDK is missing, the drill-in first offers
    to install it (:func:`_prompt_install_antigravity`). Unlike the CLI-backed harnesses
    (whose drill-in *gates* on the CLI), declining here still drops into the key menu,
    since the ``antigravity:`` key is independently storable.

    :returns: None. Side effects: may install the ``antigravity`` extra, and may write
        the ``antigravity:`` config block and the secret store.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.antigravity_auth import (
        ANTIGRAVITY_CONFIG_KEY,
        ANTIGRAVITY_SECRET_NAME,
        antigravity_api_key_configured,
        antigravity_api_key_ref,
        antigravity_sdk_installed,
    )
    from omnigent.onboarding.interactive import select

    # Offer the install once on entry (not per loop iteration); the returned status
    # seeds the menu's transient status line.
    status: str | None = None
    if not antigravity_sdk_installed():
        status = _prompt_install_antigravity()
    while True:
        config = _load_global_config()
        key_set = antigravity_api_key_configured(config)

        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow(
                "Replace Gemini API key" if key_set else "Set Gemini API key",
                action="set_key",
            )
        ]
        if key_set:
            rows.append(_HarnessMenuRow("Remove API key", action="remove_key"))
        rows.append(_HarnessMenuRow("← Back", action="back"))

        header = (
            "Antigravity — Gemini API key configured"
            if key_set
            else "Antigravity — no Gemini API key yet"
        )
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "set_key":
            status = _set_antigravity_api_key()
        elif action == "remove_key":
            ref = antigravity_api_key_ref(config)
            # Only the secret we own (``keychain:antigravity``) is ours to
            # delete: a hand-edited block may point at a shared ``keychain:<other>``
            # secret, and an ``env:`` ref names the user's own environment. In
            # both of those cases just drop the config block and leave the secret.
            if ref == f"keychain:{ANTIGRAVITY_SECRET_NAME}":
                secret_store.delete_secret(ANTIGRAVITY_SECRET_NAME)
            _save_global_config({}, unset_keys=(ANTIGRAVITY_CONFIG_KEY,))
            status = "✓ Removed Gemini API key"


def _set_antigravity_api_key() -> str | None:
    """Prompt for and store a Gemini API key; return a status line.

    Offers an existing ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY`` first
    (recorded as an ``env:`` ref, so the secret stays in the environment), else
    reads it with a hidden prompt and stores it under ``keychain:antigravity``.
    The ``AIza`` prefix is checked softly (a wrong paste is caught but can be
    forced). The key is never echoed.

    :returns: A status string for the menu, or ``None`` if the user aborted.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.antigravity_auth import (
        ANTIGRAVITY_ENV_VARS,
        ANTIGRAVITY_SECRET_NAME,
        antigravity_api_key_settings,
        looks_like_gemini_api_key,
    )
    from omnigent.onboarding.interactive import prompt_text

    detected_var = next((v for v in ANTIGRAVITY_ENV_VARS if os.environ.get(v)), None)
    if detected_var is not None and click.confirm(
        f"Detected {detected_var} in the environment — use it?", default=True
    ):
        detected = os.environ[detected_var]
        if not looks_like_gemini_api_key(detected) and not click.confirm(
            f"${detected_var} doesn't start with 'AIza'. Use it anyway?", default=False
        ):
            return None
        _save_global_config(antigravity_api_key_settings(f"env:{detected_var}"))
        return f"✓ Gemini API key set (from ${detected_var})"

    pasted = prompt_text("Gemini API key (GEMINI_API_KEY)", hide_input=True).strip()
    if not pasted:
        return None
    if not looks_like_gemini_api_key(pasted) and not click.confirm(
        "That doesn't start with 'AIza'. Store it anyway?", default=False
    ):
        return None
    secret_store.store_secret(ANTIGRAVITY_SECRET_NAME, pasted)
    _save_global_config(antigravity_api_key_settings(f"keychain:{ANTIGRAVITY_SECRET_NAME}"))
    return "✓ Gemini API key stored"


def _manage_credential(provider: str, family: str) -> str | None:
    """Run the level-3 loop for one credential: make default / remove.

    Opened by selecting a credential at level 2. Offers ``Make default`` (only
    when it is not already this harness's default), ``Remove``, and ``← Back``.
    Make-default / remove return to level 2 with a confirmation; ``← Back`` /
    Esc / ``q`` return with no change.

    :param provider: The provider id of the chosen credential, e.g. ``"openai"``.
    :param family: The harness surface in context, ``"anthropic"`` /
        ``"openai"`` / ``"pi"``.
    :returns: A confirmation string to show as a transient status at level 2,
        or ``None`` when nothing changed.
    """
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.interactive import select
    from omnigent.onboarding.provider_config import (
        DATABRICKS_KIND,
        SUBSCRIPTION_KIND,
        load_providers,
        surface_default_provider,
    )

    config = _load_global_config()
    entry = load_providers(config).get(provider)
    if entry is None:
        return None
    label = _family_credential_label(config, family, provider, entry)
    rows: list[_HarnessMenuRow] = []
    # "Make default" is offered unless this credential is already the
    # surface's *effective* default (matching the ✓ on the level-2 row) —
    # for pi that covers the fallback-driven default too, where offering
    # "make default" would be a confusing no-op.
    default = surface_default_provider(config, family)
    if default is None or default.name != provider:
        rows.append(
            _HarnessMenuRow(
                f"Make default for {family_label(family)}", action="set_default", provider=provider
            )
        )
    rows.append(_HarnessMenuRow("Remove", action="remove", provider=provider))
    rows.append(_HarnessMenuRow("← Back", action="back"))

    idx = select(label, [r.label for r in rows], clear_on_exit=True)
    if idx < 0:  # Esc / q — back to the credential list, no change
        return None
    row = rows[idx]
    if row.action == "back":
        return None
    if row.action == "set_default":
        return _set_harness_default(provider, family)
    # A subscription's credential lives in the harness CLI's own auth file, not
    # our config — so removing it means signing out of that CLI (otherwise the
    # login persists and ambient detection re-adopts it on the next open).
    if entry.kind == SUBSCRIPTION_KIND:
        return _remove_subscription(provider, family)
    # A databricks provider was wired by `ucode configure`, which edits
    # harness configs outside ~/.omnigent/config.yaml — so removing it
    # also cleans those edits up (otherwise codex keeps routing through
    # the workspace gateway).
    if entry.kind == DATABRICKS_KIND:
        return _remove_databricks_provider(provider)
    return _remove_credential(provider)


def _remove_subscription(provider: str, family: str) -> str | None:
    """Sign out of the harness CLI and remove the subscription credential.

    Unlike a key/gateway provider (whose credential is ours to drop), a
    subscription is backed by the harness CLI's own login file
    (``~/.codex/auth.json`` / ``~/.claude/.credentials.json``). Deleting only
    our entry would leave that login in place — so it would still drive the
    standalone CLI, and ambient detection would re-adopt the subscription on the
    next ``configure`` open. So "remove" here runs the harness's own logout
    (``codex logout`` / ``claude auth logout``) and then drops our entry. Guarded
    by an explicit confirm (default No) because it signs the user out of the
    standalone CLI too. (To merely stop *using* a subscription while staying
    logged in, the user makes another provider the default instead.)

    :param provider: The subscription provider id, e.g. ``"codex-subscription"``.
    :param family: The harness family, ``"anthropic"`` (Claude) / ``"openai"``
        (Codex).
    :returns: A confirmation message for the level-2 status line, or ``None``
        when the user declined (nothing changed). Side effects: runs the
        harness logout command and writes ``~/.omnigent/config.yaml``.
    """
    from omnigent.onboarding.harness_install import harness_install_spec, harness_logout
    from omnigent.onboarding.interactive import select

    spec = harness_install_spec(family)
    disp = spec.display if spec is not None else family
    logout_cmd = (
        f"{spec.binary} {' '.join(spec.logout_args)}"
        if spec is not None and spec.logout_args is not None
        else "logout"
    )
    choice = select(
        f"Remove {disp} subscription?",
        [f"Yes — sign out of {disp} and remove", "No — keep it"],
        descriptions=[
            f"Runs `{logout_cmd}`, signing you out of the standalone {disp} CLI "
            "too, then removes it here.",
            f"Leave the subscription and your {disp} login untouched.",
        ],
        default=1,  # default to the non-destructive choice
        clear_on_exit=True,
    )
    if choice != 0:
        return None
    signed_out = harness_logout(family)
    # Drop our entry regardless — the user asked to remove it. If logout failed
    # we say so, since the standalone login may persist (and be re-detected).
    _remove_credential(provider)
    if signed_out:
        return f"✓ Signed out of {disp} and removed"
    return (
        f"✓ Removed {disp} subscription — note: `{logout_cmd}` did not complete, "
        f"so you may still be signed in to the {disp} CLI"
    )


def _remove_databricks_provider(provider: str) -> str:
    """Remove a databricks provider and clean up ucode's harness wiring.

    A ``kind: databricks`` provider was wired by running ``ucode configure``
    (the add flow), which writes harness configs *outside*
    ``~/.omnigent/config.yaml`` — most damagingly, for Codex < 0.134.0 it
    rewrites the user's real ``~/.codex/config.toml`` (top-level
    ``profile = "ucode"``) so even the bare ``codex`` CLI routes through the
    workspace gateway, and ``ucode revert`` does not undo that edit. Removing
    the provider therefore undoes that wiring as part of the removal — no
    extra confirm, matching how a key provider's ``Remove`` acts immediately.
    The cleanup only ever touches ucode-namespaced artifacts (the ``profile``
    selector only when it equals ``"ucode"``; see
    :mod:`omnigent.onboarding.ucode_cleanup`), so the user's own settings
    are never at risk. Removal applies to every harness the provider
    serves — a databricks entry routes both Claude and Codex.

    :param provider: The databricks provider id, e.g. ``"databricks"``.
    :returns: A confirmation message for the level-2 status line reporting
        the removal and what wiring was cleaned (nothing extra is appended
        when no ucode wiring existed). Side effects: may edit
        ``~/.codex/config.toml``, delete ucode sidecar files, run
        ``claude mcp remove``, and write ``~/.omnigent/config.yaml``.
    """
    from omnigent.errors import OmnigentError
    from omnigent.onboarding.ucode_cleanup import remove_ucode_wiring

    cleanup_note = ""
    try:
        removal = remove_ucode_wiring()
    except (OmnigentError, OSError) as exc:
        # The entry removal below still proceeds — the user asked for it —
        # but say exactly what was left behind instead of failing silently.
        cleanup_note = f" — ucode cleanup incomplete: {exc}"
    else:
        cleaned: list[str] = []
        if removal.codex_config_stripped:
            cleaned.append("cleaned ~/.codex/config.toml")
        if removal.removed_sidecars:
            cleaned.append(f"deleted {len(removal.removed_sidecars)} ucode sidecar file(s)")
        if removal.web_search_mcp_removed:
            cleaned.append("unregistered ucode's web_search MCP")
        if cleaned:
            cleanup_note = f" — {', '.join(cleaned)}"
    removed_msg = _remove_credential(provider) or f"✓ Removed {provider}"
    return f"{removed_msg}{cleanup_note}"


def _set_harness_default(provider: str, family: str) -> str | None:
    """Make *provider* the default for *family* and persist wholesale.

    :param provider: The provider name to default, e.g. ``"openrouter"``.
    :param family: The harness surface to scope the default to,
        ``"anthropic"``, ``"openai"``, or ``"pi"`` — leaving the other
        harnesses' defaults untouched.
    :returns: A confirmation message for the caller to show as a transient
        status, or ``None`` when there was nothing to do. Side effect:
        writes ``~/.omnigent/config.yaml``.
    """
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.provider_config import load_providers, set_default_provider

    block = _load_global_config().get("providers")
    if not isinstance(block, dict):
        return None
    entry = load_providers({"providers": block}).get(provider)
    label = _credential_label(provider, entry) if entry is not None else provider
    _save_global_config({"providers": set_default_provider(block, provider, family)})
    return f"✓ {label} is now the {family_label(family)} default"


def _clear_detection_dismissal(name: str) -> None:
    """Drop *name* from the persisted ``dismissed_detections`` list, if present.

    Called when the user explicitly re-adds a previously Removed (and thus
    dismissed) ambient credential — e.g. picking the detected codex
    config.toml provider from the add menu — so the detection behaves like
    an ordinary one again.

    :param name: The detection name to un-dismiss, e.g. ``"codex-databricks"``.
    :returns: None. Side effect: writes ``~/.omnigent/config.yaml`` when the
        name was dismissed; no write otherwise.
    """
    from omnigent.onboarding.detected import (
        DISMISSED_DETECTIONS_KEY,
        dismissed_detection_names,
    )

    dismissed = dismissed_detection_names(_load_global_config())
    if name not in dismissed:
        return
    _save_global_config({DISMISSED_DETECTIONS_KEY: sorted(dismissed - {name})})


def _remove_credential(provider: str) -> str | None:
    """Remove the *provider* credential and persist wholesale.

    The stored secret (if any) is left in place — removing a credential does
    not assume its key is unwanted.

    :param provider: The provider id to remove, e.g. ``"openrouter"``.
    :returns: A confirmation message for the caller to show as a transient
        status, or ``None`` when there was nothing to remove. Side effect:
        writes ``~/.omnigent/config.yaml`` (and, when the removed entry is
        backed by a live ambient detection that cannot be signed out,
        records its name under ``dismissed_detections`` so the next
        configure open does not silently re-adopt it).
    """
    from omnigent.onboarding.ambient import detect_providers
    from omnigent.onboarding.detected import (
        DISMISSED_DETECTIONS_KEY,
        dismissed_detection_names,
    )
    from omnigent.onboarding.provider_config import load_providers

    config = _load_global_config()
    block = config.get("providers")
    if not isinstance(block, dict) or provider not in block:
        return None
    entry = load_providers({"providers": block}).get(provider)
    label = _credential_label(provider, entry) if entry is not None else provider
    remaining = {k: v for k, v in block.items() if k != provider}
    settings: dict[str, Any] = {"providers": remaining}  # type: ignore[explicit-any]  # yaml-boundary mapping
    # If a live ambient detection backs this entry, removing the entry alone
    # is a no-op: the next configure open re-detects and re-adopts it (the
    # "Remove doesn't remove" bug). Subscriptions are exempt — their removal
    # path signs out of the CLI instead, and a future re-login SHOULD
    # re-adopt. Everything else (env API key, codex config.toml provider,
    # local Ollama) gets a persisted dismissal that the add menu's detected
    # option clears on re-add.
    backing = next(
        (d for d in detect_providers() if d.name == provider and d.kind != "subscription"),
        None,
    )
    if backing is not None:
        settings[DISMISSED_DETECTIONS_KEY] = sorted(dismissed_detection_names(config) | {provider})
    _save_global_config(settings)  # wholesale replace per key
    if backing is not None:
        return f"✓ Removed {label} — it stays on your machine but won't be auto-configured again"
    return f"✓ Removed {label}"


def _run_configure_harnesses_interactive() -> None:
    """Run the interactive model/credential three-level picker.

    Invoked by ``omnigent setup --no-internal-beta`` and the bare-``run``
    first-run path, so both drive the identical flow.
    Opening it backfills a legacy databricks ``auth:`` block into a real
    provider and adopts any ambient-detected credential — announcing the
    newly auto-configured machine credentials in a callout — then loops on
    the level-1 harness overview (Claude / Codex / Pi / Quit) until the
    user quits or presses Esc.

    :returns: None. Side effect: may write ``~/.omnigent/config.yaml`` via
        the backfill/adopt steps and any add/set-default/remove the user
        performs while navigating.
    """
    from omnigent.onboarding.antigravity_auth import (
        ANTIGRAVITY_ENV_VARS,
        ANTIGRAVITY_EXTRA_INSTALL_COMMAND,
        antigravity_api_key_configured,
        antigravity_sdk_installed,
    )
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.cursor_auth import cursor_api_key_configured
    from omnigent.onboarding.harness_install import CURSOR_KEY, harness_cli_installed
    from omnigent.onboarding.interactive import select
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        PI_SURFACE,
        surface_default_provider,
    )

    # Surface missing external tooling (Node ≥22.10 / tmux) the harnesses need,
    # once up front, so configuring a credential doesn't lead to a cryptic
    # failure when the harness later can't launch.
    _warn_missing_harness_dependencies()

    # Backfill a databricks provider from a legacy global auth: block FIRST (it
    # outranks ambient detection in routing), then adopt ambient detections.
    # The databricks backfill is silent (it just shows up in the harness summary
    # line); newly-adopted machine credentials get a one-time callout naming
    # what was auto-configured and from where. The detection scan can take a
    # beat (on macOS it shells out to ``claude auth status`` to read the
    # Keychain), so surface a spinner over just that step — it clears before the
    # callout (and the menu) paints, and is a no-op off a TTY.
    from omnigent._runner_startup import runner_startup_progress

    with runner_startup_progress(
        initial_message="Searching for existing credentials…"
    ) as progress:
        _adopt_ambient_credentials(progress=progress)

    # Level 1: pick a harness. The cursor moves between Claude, Codex, Pi, and
    # Quit; each harness's status renders as a non-selectable sub-line beneath
    # it (skipped by ↑/↓). Drilling in (level 2) keeps add/manage off this
    # overview. The menu clears in place on each choice so the session stays on
    # one screen. Quit / Esc / q exits.
    _QUIT = "\x00quit"  # sentinel marking the Quit row (not a family)
    # Sentinel marking the Antigravity row — it is not a provider family (Gemini
    # is outside the anthropic/openai machinery), so it dispatches to its own
    # credential manager rather than ``_manage_harness_providers``.
    _ANTIGRAVITY = "\x00antigravity"
    families = [ANTHROPIC_FAMILY, OPENAI_FAMILY, PI_SURFACE]
    while True:
        config = _load_global_config()
        options: list[str] = []
        selectable: list[bool] = []
        row_target: list[str | None] = []
        for fam in families:
            # A harness's readiness is a single descent: is the CLI installed? →
            # does it have a usable default credential? → show that credential.
            # Only a fully ready harness carries no name-level marker (its green
            # default line in the summary already says it's ready); any harness
            # that can't be used yet — not installed, or installed but with no
            # usable default — gets a red ✗, so it's clear at a glance which
            # harnesses still need attention. Pi's default is its *effective*
            # one (explicit pi scope, else the cross-family fallback).
            installed = harness_cli_installed(fam)
            ready = installed and surface_default_provider(config, fam) is not None
            marker = "  " if ready else "[red]✗[/] "
            options.append(f"{marker}{family_label(fam)}")
            selectable.append(True)
            row_target.append(fam)
            # Sub-line text follows the same descent. An uninstalled harness
            # points at the install command (creds are moot until it exists);
            # otherwise the summary helper renders "no credential yet" / "no
            # default set" / the ✓ default line.
            if not installed:
                # Parallel to "no credential yet — open to add one": name the
                # state, point at the action. The exact ``npm install`` command
                # is shown on drill-in (``_prompt_install_harness``), so it stays
                # off the overview — keeping the line short enough not to wrap.
                sub_lines = ["[dim]not installed yet — open to install[/]"]
            else:
                sub_lines = _harness_summary_lines(config, fam)
            for sub_line in sub_lines:
                # Indent every status sub-line a touch more than the harness
                # name so it reads as hanging off the marker column — the
                # configured default's ✓ (and the "not installed" / "no
                # credential yet" hints) all start at the same column.
                options.append(f"  {sub_line}")
                selectable.append(False)  # a sub-line — cursor skips it
                row_target.append(None)
        # Cursor: runs via the ``cursor-sdk`` package and authenticates with a
        # ``CURSOR_API_KEY`` (the SDK requires one; it has no provider/gateway
        # family and a ``cursor-agent login`` does not apply). So readiness is
        # simply whether an API key is configured — one stored by setup (the
        # ``cursor:`` block) or inherited from the environment — and its
        # drill-in manages exactly that key.
        cursor_key_set = cursor_api_key_configured(config) or bool(
            os.environ.get("CURSOR_API_KEY")
        )
        options.append(f"{'  ' if cursor_key_set else '[red]✗[/] '}Cursor")
        selectable.append(True)
        row_target.append(CURSOR_KEY)
        cursor_sub = (
            "[green]✓[/] API key configured"
            if cursor_key_set
            else "[dim]no API key yet — open to add one[/]"
        )
        options.append(f"  {cursor_sub}")
        selectable.append(False)
        row_target.append(None)
        # Antigravity (Gemini-native, no provider family): like Cursor, readiness
        # is just whether a Gemini key is configured (``antigravity:`` block or
        # ambient env); its drill-in manages that key. Vertex specs need no key,
        # so a ✗ isn't a hard blocker for that path.
        ag_key_set = antigravity_api_key_configured(config) or any(
            os.environ.get(v) for v in ANTIGRAVITY_ENV_VARS
        )
        options.append(f"{'  ' if ag_key_set else '[red]✗[/] '}Antigravity")
        selectable.append(True)
        row_target.append(_ANTIGRAVITY)
        # The antigravity SDK ships in an OPTIONAL extra (unlike Cursor's baseline
        # ``cursor-sdk``), so a user can have a key but no SDK. Lead with that gap when
        # the extra is missing — naming the install command inline — then still report
        # key status. ``[antigravity]`` is escaped since the sub-lines render as Rich
        # markup (bare brackets parse as a tag).
        ag_sub_lines: list[str] = []
        if not antigravity_sdk_installed():
            from rich.markup import escape as _rich_escape

            ag_sub_lines.append(
                f"[dim]not installed — open to install "
                f"({_rich_escape(ANTIGRAVITY_EXTRA_INSTALL_COMMAND)})[/]"
            )
        ag_sub_lines.append(
            "[green]✓[/] Gemini API key configured"
            if ag_key_set
            else "[dim]no Gemini API key yet — open to add one[/]"
        )
        for ag_sub in ag_sub_lines:
            options.append(f"  {ag_sub}")
            selectable.append(False)
            row_target.append(None)
        options.append("Quit")
        selectable.append(True)
        row_target.append(_QUIT)
        idx = select(
            "Configure harnesses",
            options,
            selectable=selectable,
            clear_on_exit=True,
        )
        if idx < 0:  # Esc / q — exit
            return
        target = row_target[idx]
        if target == CURSOR_KEY:
            _manage_cursor_harness()
        elif target in families:
            _manage_harness_providers(target)
        elif target == _ANTIGRAVITY:
            _manage_antigravity_harness()
        else:  # Quit row (or, defensively, a non-family row)
            return


