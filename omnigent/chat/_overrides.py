"""Implementation of the ``omnigent chat`` command.

The CLI always ends by connecting an Omnigent client to a server URL. For
path targets it first ensures the agent is registered on that server
(a local subprocess by default, or ``--server`` when supplied). URL
targets skip setup and use the existing server's registered agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

import click
import httpx
import yaml
from omnigent_client import (
    OmnigentClient,
    SessionToolCallInfo,
    ToolCallable,
    ToolCallInfo,
    ToolHandler,
)
from omnigent_client import (
    OmnigentError as ClientOmnigentError,
)
from omnigent_client._events import (
    ErrorEvent,
    ResponseCancelled,
    ResponseCompleted,
    ResponseFailed,
    ResponseIncomplete,
    TextDelta,
)
from rich.console import Console

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
)
from omnigent.conversation_browser import open_conversation_link_if_enabled
from omnigent.errors import OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.inner.databricks_executor import _DatabricksBearerAuth, _read_databrickscfg
from omnigent.native_coding_agents import native_coding_agent_for_wrapper_label
from omnigent.spec import load as load_spec
from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, SkillSpec

if TYPE_CHECKING:
    from omnigent._runner_startup import RunnerStartupProgress

console = Console()

# YAML mapping shape — heterogeneous JSON-shaped values
# (strings, ints, lists, nested dicts) so ``Any`` is the
# narrowest safe element type. Used as the parsed-spec
# return / input shape across this module's helpers.
_YamlMapping: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

logger = logging.getLogger(__name__)

# Local server readiness polling: use a short initial interval so
# freshly-launched ``omnigent run`` sessions don't burn a
# fixed 500 ms before noticing the server is ready, then back off
# slightly while still remaining responsive on slower cold starts.
_SERVER_READY_INITIAL_POLL_SECONDS = 0.05
_SERVER_READY_BACKOFF_POLL_SECONDS = 0.1
_SERVER_READY_FAST_POLL_WINDOW_SECONDS = 1.0

# Remote ``--server`` runners are disposable subprocesses created for
# the CLI session. A one-second grace gives SIGTERM enough time to
# flush runner logs and unregister without noticeably slowing CLI exit.
# Grace period before the CLI escalates SIGTERM → SIGKILL on the
# runner subprocess. Must be long enough for the runner's shutdown
# chain to complete: cancel async tasks → app.router.shutdown() →
# _stop_pm() → _terminal_registry.shutdown() → tmux kill-server
# per session → pm.shutdown() → SIGTERM each harness. 1 s was too
# short — the runner was SIGKILL'd before tmux sessions were reaped,
# leaving zombie codex/claude processes.
_REMOTE_RUNNER_STOP_GRACE_SECONDS = 8.0

# Fallback model when the YAML declares neither ``executor.model``
# nor ``executor.harness`` AND no ``--model`` / ``--harness``
# override is supplied. Mirrors the legacy argparse CLI's
# ``_DEFAULT_AD_HOC_MODEL`` so ``omnigent run examples/hello_world.yaml``
# (a spec with no executor block) launches cleanly instead of
# failing the strict omnigent validator with a cryptic
# "executor.config.harness: required" error.
_DEFAULT_AD_HOC_MODEL = "databricks-gpt-5-4"

# How many of the NEWEST transcript items ``_persisted_turn_text``
# fetches when reconciling a headless ``-p`` turn against the durable
# store. The current turn's items are always the newest, and no single
# one-shot turn emits anywhere near this many items, so the latest turn
# is fully captured regardless of how long a resumed session's history
# is. Fetched ``order="desc"`` (newest first) precisely so the window
# tracks the end of the conversation, not its start.
_RECONCILE_ITEMS_LIMIT = 100

# Optional bearer token for remote omnigent servers that sit
# behind an auth proxy (for example Databricks Apps). When set, the
# CLI sends ``Authorization: Bearer <value>`` on every HTTP request it
# makes to the remote server.
_REMOTE_AUTH_TOKEN_ENV = "OMNIGENT_REMOTE_AUTH_TOKEN"

# Env-var override name. ``OMNIGENT_MODEL=foo`` lets a user
# pin a default model per shell session without needing to pass
# ``--model foo`` on every invocation. Resolved once at spec
# materialization time (not at runtime), so the materialized
# bundle stays self-contained — identical behavior on any host
# that runs the bundle, regardless of that host's env. Mirrors
# the legacy ``_default_cli_model`` at
# ``omnigent/inner/cli.py:344``.
_OMNIGENT_MODEL_ENV_VAR = "OMNIGENT_MODEL"
_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_OPENAI_BASE_URL_ENV_VAR = "OPENAI_BASE_URL"
_OPENAI_AGENTS_HARNESSES = frozenset({"openai-agents", "openai-agents-sdk"})
_MATERIALIZED_OVERRIDE_DIRS: dict[Path, Path] = {}


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _materialize_override_bundle(source: Path, overrides: ChatOverrides) -> Path:
    """
    Copy *source* into a temp dir and apply CLI overrides to its YAML.

    Also materializes when the spec is a single-file YAML with no
    ``executor.harness`` AND no ``executor.model`` — the strict
    omnigent validator rejects that shape, and the legacy
    argparse CLI used to paper over it by injecting
    :data:`_DEFAULT_AD_HOC_MODEL`. This preserves that behavior so
    ``omnigent run examples/hello_world.yaml`` (minimal spec) still
    launches cleanly.

    When no override is set and the spec already declares harness or
    model, returns *source* unchanged — no temp materialization.

    :param source: Path to the agent YAML or directory.
    :param overrides: CLI overrides. All-None means "no user
        override"; a default-model fallback may still apply.
    :returns: Path that the server should register — either the
        original *source* or a rewritten copy under a tempdir.
    """
    raw_peek = _load_yaml_if_single_file(source)
    raw_override_peek = _load_yaml_for_override_peek(source)
    needs_fallback = raw_peek is not None and not _spec_declares_harness_or_model(raw_peek)
    needs_openai_env_auth = raw_override_peek is not None and _should_materialize_openai_env_auth(
        raw_override_peek, overrides
    )
    if not overrides.has_any and not needs_fallback and not needs_openai_env_auth:
        return source

    # ``_cleanup_materialized_override_bundle`` removes this tempdir
    # once validation, bundling, or the attached REPL/server path no
    # longer needs the rewritten spec.
    tmpdir = Path(tempfile.mkdtemp(prefix="omnigent-override-"))
    try:
        if source.is_file():
            target = tmpdir / source.name
            target.write_bytes(source.read_bytes())
        else:
            # Copy the whole bundle so bundled tools / sub-agent dirs /
            # skills travel with the rewritten config.yaml. The user's
            # source tree is never touched.
            target_dir = tmpdir / source.name
            shutil.copytree(source, target_dir)
            config = target_dir / "config.yaml"
            if not config.is_file():
                raise click.ClickException(f"{source}: directory has no config.yaml to override.")
            target = config

        raw = yaml.safe_load(target.read_text())
        if not isinstance(raw, dict):
            raise click.ClickException(
                f"{source}: expected YAML mapping at top level, got {type(raw).__name__}"
            )
        _apply_overrides_to_raw(raw, overrides)
        target.write_text(yaml.safe_dump(raw, default_flow_style=False))
        materialized = target if source.is_file() else target.parent
        _MATERIALIZED_OVERRIDE_DIRS[materialized.resolve()] = tmpdir
        return materialized
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

def _cleanup_materialized_override_bundle(materialized: Path) -> None:
    """
    Remove the temp directory created for a materialized override bundle.

    Override materialization can bake provider credentials from the CLI
    environment into the copied YAML before bundling. The copy is needed
    only while the caller validates, starts a local server, or uploads the
    bundle, so delete the tempdir explicitly instead of leaving secrets for
    OS temp reaping.

    :param materialized: Path returned by
        :func:`_materialize_override_bundle`, e.g.
        ``Path("/tmp/omnigent-override-abc/agent.yaml")``.
    :returns: None.
    """
    tempdir = _MATERIALIZED_OVERRIDE_DIRS.pop(materialized.resolve(), None)
    if tempdir is None:
        return
    shutil.rmtree(tempdir, ignore_errors=True)

def _load_yaml_for_override_peek(source: Path) -> _YamlMapping | None:
    """
    Load the YAML that override materialization would rewrite.

    Single-file specs rewrite the file itself. Agent-image
    directories rewrite ``config.yaml``. Invalid or non-mapping YAML
    returns ``None`` so the normal validation path can surface the
    precise user-facing error later.

    :param source: Path to a YAML file or agent directory.
    :returns: Parsed top-level YAML mapping, or ``None`` when no
        rewrite target can be inspected.
    """
    if source.is_dir():
        config = source / "config.yaml"
        if not config.is_file():
            return None
        parsed = yaml.safe_load(config.read_text())
        return parsed if isinstance(parsed, dict) else None
    return _load_yaml_if_single_file(source)

def _load_yaml_if_single_file(source: Path) -> _YamlMapping | None:
    """
    Load the YAML at *source* if it's a single-file spec; else None.

    Directories (omnigent-style with ``config.yaml``) are handled
    separately by the materializer — this helper just peeks at the
    single-file case so the caller can decide whether the
    default-model fallback applies.

    :param source: Path to a YAML file or agent directory.
    :returns: Parsed top-level dict, or None if *source* is a
        directory or the YAML isn't a mapping.
    """
    if not source.is_file():
        return None
    parsed = yaml.safe_load(source.read_text())
    return parsed if isinstance(parsed, dict) else None

def _spec_declares_harness_or_model(raw: _YamlMapping) -> bool:
    """
    True when the YAML's ``executor:`` block has harness or model.

    Either signal is enough for the spec-adapter's harness auto-pick
    (``databricks-claude-*`` → ``claude-sdk``, etc.) — the
    default-model fallback only kicks in when BOTH are absent.

    Recognizes the harness in either shape: a flat ``executor.harness``
    or the bundle-style nested ``executor.config.harness`` (e.g.
    ``examples/polly``). Without the nested check, an unpinned bundle
    that declares its harness only under ``config`` would look
    harness-less and get force-fed :data:`_DEFAULT_AD_HOC_MODEL` — a
    GPT endpoint the claude-sdk harness can't speak.

    :param raw: Parsed top-level YAML mapping.
    :returns: True if ``executor.harness``, ``executor.model``, or
        ``executor.config.harness`` is a non-empty value.
    """
    executor = raw.get("executor")
    if not isinstance(executor, dict):
        return False
    if executor.get("harness") or executor.get("model"):
        return True
    config = executor.get("config")
    return isinstance(config, dict) and bool(config.get("harness"))

def _should_materialize_openai_env_auth(
    raw: _YamlMapping,
    overrides: ChatOverrides,
) -> bool:
    """
    Return whether materialization would inject OpenAI env credentials.

    Daemon-backed local runs launch a daemon-owned runner whose
    environment intentionally strips provider secrets. Specs that rely
    only on ambient ``OPENAI_API_KEY`` therefore need those credentials
    baked into ``executor.auth`` before bundling, but only when the
    effective harness is OpenAI-compatible and no explicit spec/provider
    auth already wins.

    :param raw: Parsed top-level YAML mapping.
    :param overrides: CLI overrides that will be applied to ``raw``.
    :returns: ``True`` when a rewritten bundle is needed solely to add
        ``executor.auth`` from ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``.
    """
    executor = raw.get("executor")
    executor_block = executor if isinstance(executor, dict) else {}
    harness = _effective_openai_auth_harness(raw, executor_block, overrides)
    return _should_inject_openai_env_auth_for_executor(executor_block, harness)

def _effective_openai_auth_harness(
    raw: _YamlMapping,
    executor_block: _YamlMapping,
    overrides: ChatOverrides,
) -> str | None:
    """
    Resolve the harness relevant to OpenAI env-auth injection.

    This mirrors the edge of the normal spec path closely enough for
    materialization decisions: explicit CLI harness wins, then YAML
    harness, then model-prefix inference from the effective model.

    :param raw: Parsed top-level YAML mapping.
    :param executor_block: Parsed ``executor`` mapping from ``raw``.
    :param overrides: CLI overrides that will be applied.
    :returns: Canonical harness name, e.g. ``"openai-agents"``, or
        ``None`` when no harness can be inferred.
    """
    raw_harness = executor_block.get("harness")
    harness = overrides.harness if overrides.harness is not None else raw_harness
    if isinstance(harness, str) and harness:
        return canonicalize_harness(harness) or harness

    model = _effective_openai_auth_model(raw, executor_block, overrides)
    if model is None:
        return None
    from omnigent.llms.routing import infer_harness_from_model

    inferred = infer_harness_from_model(model)
    return inferred or None

def _effective_openai_auth_model(
    raw: _YamlMapping,
    executor_block: _YamlMapping,
    overrides: ChatOverrides,
) -> str | None:
    """
    Resolve the model relevant to OpenAI env-auth injection.

    :param raw: Parsed top-level YAML mapping.
    :param executor_block: Parsed ``executor`` mapping from ``raw``.
    :param overrides: CLI overrides that will be applied.
    :returns: Effective model string, e.g. ``"databricks-gpt-5-4-mini"``,
        or ``None`` when neither CLI nor YAML names a model.
    """
    if overrides.model is not None:
        return overrides.model
    raw_model = executor_block.get("model")
    if isinstance(raw_model, str) and raw_model:
        return raw_model
    llm = raw.get("llm")
    if isinstance(llm, dict):
        llm_model = llm.get("model")
        if isinstance(llm_model, str) and llm_model:
            return llm_model
    return None

def _inject_openai_env_auth_if_needed(raw: _YamlMapping) -> None:
    """
    Add explicit OpenAI-compatible auth to ``raw`` when env fallback is unsafe.

    Daemon-owned runners do not inherit provider secret environment
    variables. For an OpenAI-compatible harness that otherwise relies on
    ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``, bake the resolved values into
    the materialized spec so the uploaded bundle remains self-contained.

    :param raw: Parsed top-level YAML mapping, mutated in place.
    :returns: None.
    """
    executor = raw.get("executor")
    executor_block: _YamlMapping
    if isinstance(executor, dict):
        executor_block = executor
    else:
        executor_block = {}
        raw["executor"] = executor_block
    harness = _effective_openai_auth_harness(raw, executor_block, ChatOverrides())
    if not _should_inject_openai_env_auth_for_executor(executor_block, harness):
        return
    auth: dict[str, str] = {
        "type": "api_key",
        "api_key": os.environ[_OPENAI_API_KEY_ENV_VAR],
    }
    base_url = os.environ.get(_OPENAI_BASE_URL_ENV_VAR)
    if base_url:
        auth["base_url"] = base_url
    executor_block["auth"] = auth

def _apply_overrides_to_raw(raw: _YamlMapping, overrides: ChatOverrides) -> None:
    """
    Mutate *raw* to reflect CLI overrides + the default-model fallback.

    Mirrors the legacy argparse CLI's ``_apply_overrides_to_yaml``
    so behavior is unchanged post-unification. The harness override is
    format-aware — see :func:`_apply_harness_override_to_executor`.

    :param raw: Parsed YAML mapping (mutated in place).
    :param overrides: CLI overrides to bake into the ``executor``
        block.
    """
    executor_block = raw.get("executor")
    if not isinstance(executor_block, dict):
        executor_block = {}
        raw["executor"] = executor_block
    if overrides.model is not None:
        executor_block["model"] = overrides.model
    if overrides.harness is not None:
        _apply_harness_override_to_executor(raw, executor_block, overrides.harness)
    # When neither harness nor model is declared — after overrides —
    # inject the ad-hoc default. Gated on harness absence so a YAML
    # like ``claude_code_agent.yaml`` (declares harness, no model)
    # doesn't get silently paired with the gpt-5-4 default, which
    # the Databricks FM API rejects for Claude-typed entities.
    # Uses ``_spec_declares_harness_or_model`` — must agree with the
    # ``needs_fallback`` gate in :func:`_materialize_override_bundle`.
    # Uses ``_default_cli_model`` (env-var-aware) instead of
    # ``_DEFAULT_AD_HOC_MODEL`` directly so ``OMNIGENT_MODEL=foo``
    # is honored on the ``omnigent/cli.py`` → ``run_chat`` direct
    # path. Without this, that env var was silently dropped on the
    # Omnigent path invoked through the ``omnigent`` console
    # script (see ``designs/RUN_OMNIGENT_REPL_PARITY.md``).
    if not _spec_declares_harness_or_model(raw):
        executor_block["model"] = _default_cli_model()
    _inject_openai_env_auth_if_needed(raw)
    if overrides.system_prompt is not None:
        raw["prompt"] = overrides.system_prompt

def _apply_harness_override_to_executor(
    raw: _YamlMapping,
    executor_block: _YamlMapping,
    harness: str,
) -> None:
    """
    Write the ``--harness`` override where the spec's format reads it.

    Single-file omnigent YAMLs (``name`` + ``prompt``, no
    ``spec_version``) read the flat ``executor.harness`` key.
    ``spec_version`` bundles (e.g. ``examples/polly``) read ONLY
    ``executor.config.harness`` — writing the flat key there is a
    silent no-op, which made ``omnigent run examples/polly
    --harness pi`` keep the claude-sdk brain.

    :param raw: Parsed top-level YAML mapping (used to detect the
        spec format via the ``spec_version`` discriminator).
    :param executor_block: The ``executor:`` mapping inside *raw*
        (mutated in place).
    :param harness: The ``--harness`` value, e.g. ``"pi"``.
    :raises click.ClickException: If a ``spec_version`` bundle
        declares a non-omnigent ``executor.type`` — those executors
        have no ``config.harness``, so the override cannot apply.
    """
    canonical = canonicalize_harness(harness) or harness
    # "spec_version" is the format discriminator (see is_omnigent_yaml).
    if "spec_version" not in raw:
        executor_block["harness"] = canonical
        return
    etype = str(executor_block.get("type", OMNIGENT_EXECUTOR_TYPE))
    if etype != OMNIGENT_EXECUTOR_TYPE:
        raise click.ClickException(
            f"--harness only applies to specs with executor.type "
            f"{OMNIGENT_EXECUTOR_TYPE!r}; this spec declares executor.type {etype!r}."
        )
    config = executor_block.get("config")
    if not isinstance(config, dict):
        config = {}
        executor_block["config"] = config
    config["harness"] = canonical

def _validate_agent_spec(agent_path: Path) -> None:
    """
    Parse and validate the agent spec in this process.

    Mirrors the work the server subprocess will do at startup so that
    config errors surface as a clean ``ClickException`` here instead
    of being swallowed by the server's silenced stderr (see
    ``_start_local_server``). Both ``OmnigentError`` (parse/
    validation/env-expansion failures) and ``FileNotFoundError``
    (missing ``config.yaml``) are converted; everything else
    propagates so genuine bugs aren't masked.

    :param agent_path: Path to the agent directory,
        e.g. ``Path("examples/archer")``.
    :raises click.ClickException: If the spec is missing, malformed,
        or references unresolved environment variables.
    """
    try:
        load_spec(agent_path)
    except (OmnigentError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc

def _extract_agent_name(agent_path: Path) -> str:
    """
    Resolve the display name for the REPL banner.

    Accepts both agent-image directories and standalone omnigent
    YAML files. The heavy lifting lives in
    :func:`omnigent.spec.load`, which dispatches on the source
    shape and returns a validated :class:`AgentSpec` whose ``name``
    field is the authoritative label. On any load failure (missing
    ``config.yaml``, malformed YAML, unresolved ``${VAR}``
    references) fall back to a filesystem-derived label so the
    banner always prints — the server subprocess will surface the
    real error moments later.

    :param agent_path: Path to an agent directory or standalone
        omnigent YAML file.
    :returns: The agent name for REPL display.
    """
    try:
        return load_spec(agent_path).name or _fallback_label(agent_path)
    except (OmnigentError, FileNotFoundError):
        # Server subprocess will surface the real error; give the
        # banner SOMETHING to show in the meantime.
        return _fallback_label(agent_path)

def _merge_host_skills(
    agent_spec: AgentSpec,
    spec_path: Path,
) -> list[SkillSpec]:
    """
    Merge bundled skills with host-scope skills for the REPL.

    Discovers ``.claude/skills/`` and ``.agents/skills/`` walking
    up from the agent root, deduplicates by name (bundled wins),
    and returns the combined list.

    :param agent_spec: Parsed AgentSpec with ``.skills`` and
        ``.skills_filter``.
    :param spec_path: Path to the agent YAML or directory.
    :returns: Combined skill list, or empty list.
    """
    bundled: list[SkillSpec] = agent_spec.skills or []
    skills_filter = agent_spec.skills_filter
    agent_root = spec_path if spec_path.is_dir() else spec_path.parent
    host = discover_host_skills(agent_root, skills_filter)
    bundled_names = {s.name for s in bundled}
    merged = list(bundled)
    for hs in host:
        if hs.name not in bundled_names:
            merged.append(hs)
    return merged

def _fallback_label(agent_path: Path) -> str:
    """
    Derive a reasonable display label from a path when the spec
    didn't supply one.

    Directories use the directory name (the standard AGENTSPEC.md
    convention). Files use the stem — e.g. ``foo.yaml`` → ``"foo"``
    — rather than the full filename so the banner doesn't carry
    redundant extensions.

    :param agent_path: Path to an agent directory or YAML file.
    :returns: A human-readable label.
    """
    return agent_path.stem if agent_path.is_file() else agent_path.name

def _canonicalize_local_agent_path(agent_path: Path) -> Path:
    """
    Normalize a local agent path before materialization and bundling.

    Directory-agent bundles are commonly invoked via their root
    ``config.yaml``. Treating that file as a standalone YAML would drop
    sibling directories such as ``agents/`` and ``skills/`` from the
    uploaded bundle, so canonicalize it to the bundle root.

    :param agent_path: Existing local path supplied to ``omnigent run``,
        e.g. ``Path("examples/polly/config.yaml")``.
    :returns: The bundle root for root ``config.yaml`` paths, otherwise
        the original path.
    """
    if agent_path.is_file() and agent_path.name == "config.yaml":
        return agent_path.parent
    return agent_path


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _native as _sib_native
    from . import _remote as _sib_remote
    from . import _repl as _sib_repl
    from . import _server_proc as _sib_server_proc
    from . import _sessions as _sib_sessions
    from . import _types as _sib_types
    for _key, _value in _sib_daemon.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_local.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_native.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_remote.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_repl.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_server_proc.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_sessions.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_types.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
