"""Parse an agent image directory into an AgentSpec."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.datamodel import (
    DEFAULT_BASIC_USERNAME,
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    OSEnvSandboxSpec,
    OSEnvSpec,
    TerminalEnvSpec,
)
from omnigent.spec.types import (
    DEFAULT_ASK_TIMEOUT,
    AgentSpec,
    ApiKeyAuth,
    BlueprintLoopSpec,
    BlueprintNode,
    BlueprintSpec,
    BuiltinToolConfig,
    CompactionConfig,
    DatabricksAuth,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    InteractionConfig,
    LabelDef,
    LLMConfig,
    LocalToolInfo,
    MCPOAuthConfig,
    MCPServerConfig,
    ModalityConfig,
    Phase,
    PhaseSelector,
    PolicyAction,
    PolicySpec,
    ProviderAuth,
    RetryPolicy,
    SandboxConfig,
    SkillSpec,
    ToolsConfig,
)

_log = logging.getLogger(__name__)

# Context files scanned in priority order when ``instructions:`` is absent.
# First file found wins (no merge).
_CONTEXT_FILE_PRIORITY: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".cursorrules")

# Pattern for SKILL.md YAML frontmatter delimited by ---
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


# Allowed tool ``type`` values when the supervisor harness is
# selected (``config.harness == "databricks_supervisor"``). Each entry maps the
# tool type to its required field names — the parser enforces both
# membership and required fields. Lives at the top of the module so
# they are easy to grep and so two functions cannot independently
# duplicate the same set.
#
# Adding a new tool type is a one-line change here plus a parser
# test — no runtime, harness, or workflow code touches needed. See
# ``designs/DATABRICKS_SUPERVISOR_API_INTEGRATION.md`` for the recipe and the
# rationale for why these tools are Databricks-resident only.
_SUPERVISOR_TOOL_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "genie_space": frozenset({"id", "description"}),
    "uc_function": frozenset({"name", "description"}),
    "uc_connection": frozenset({"name", "description"}),
    "app": frozenset({"name", "description"}),
    "knowledge_assistant": frozenset({"knowledge_assistant_id", "description"}),
    "uc_table": frozenset({"table_name", "description"}),
    "volume": frozenset({"name", "description"}),
}


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _parse_os_env(
    raw: object,
) -> OSEnvSpec | None:
    """
    Parse the top-level ``os_env:`` block into an :class:`OSEnvSpec`.

    Native Omnigent YAML mirrors the omnigent YAML shape so users
    moving from one to the other don't have to relearn the
    config surface — a top-level ``os_env:`` mapping with
    ``type``, ``cwd``, ``sandbox: {...}``, ``fork``, and
    ``start_in_scratch`` keys. See
    :class:`omnigent.inner.datamodel.OSEnvSpec` for the
    semantics of each field.

    :param raw: The raw ``os_env:`` value from config.yaml.
        Either a mapping (parsed) or absent (``None``).
        Example: ``{"type": "caller_process", "cwd": ".",
        "sandbox": {"type": "linux_bwrap",
        "write_paths": ["."], "allow_network": False}}``.
    :returns: A populated :class:`OSEnvSpec` when the block is
        present, ``None`` when absent.
    :raises OmnigentError: If *raw* is not a mapping, or
        ``start_in_scratch`` is set together with ``fork`` (those
        knobs both manage the agent's writable workspace and would
        fight each other), or ``start_in_scratch`` is set on a
        spec whose ``sandbox.type`` is ``"none"`` (no scratch
        tmpdir is created in that case so there is nothing to
        chdir into).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"os_env must be a YAML mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    sandbox = _parse_os_env_sandbox(raw.get("sandbox"))
    cwd_raw = raw.get("cwd")
    fork = bool(raw.get("fork", False))
    start_in_scratch = bool(raw.get("start_in_scratch", False))
    if start_in_scratch and fork:
        raise OmnigentError(
            "os_env.start_in_scratch and os_env.fork are mutually exclusive: "
            "fork already provides a writable workspace by copying cwd",
            code=ErrorCode.INVALID_INPUT,
        )
    if start_in_scratch and sandbox is not None and sandbox.type == "none":
        raise OmnigentError(
            "os_env.start_in_scratch requires an active sandbox; "
            "sandbox.type=none does not create a scratch tmpdir",
            code=ErrorCode.INVALID_INPUT,
        )
    return OSEnvSpec(
        type=str(raw.get("type", "caller_process")),
        cwd=str(cwd_raw) if cwd_raw is not None else None,
        sandbox=sandbox,
        fork=fork,
        start_in_scratch=start_in_scratch,
    )

def _parse_terminals(
    raw: object,
) -> dict[str, TerminalEnvSpec] | None:
    """
    Parse the top-level ``terminals:`` block into a map of
    :class:`TerminalEnvSpec`.

    Native Omnigent YAML mirrors the omnigent-compat ``terminals:`` shape — a
    mapping of ``terminal_name`` → ``{command, args, env, os_env,
    allow_cwd_override, allow_sandbox_override, scrollback, ...}`` — so a
    bundle agent registers the ``sys_terminal_*`` toolkit exactly like a
    compat agent. Closes the native-YAML gap left as additive follow-up in
    ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §3 (``_parse_terminals`` parallel to
    ``_parse_os_env``).

    :param raw: The raw ``terminals:`` value from config.yaml — a mapping of
        terminal name → config, or absent (``None``). Example:
        ``{"claude_code": {"command": "isaac", "allow_cwd_override": True,
        "os_env": {"type": "caller_process", "sandbox": {"type": "none"}}}}``.
    :returns: Map of terminal name → :class:`TerminalEnvSpec` when present and
        non-empty, else ``None`` (so ``sys_terminal_*`` stays unregistered).
    :raises OmnigentError: If ``terminals`` (or any entry) is not a mapping,
        or an entry's ``args`` / ``env`` are the wrong type.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"terminals must be a YAML mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    terminals: dict[str, TerminalEnvSpec] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise OmnigentError(
                f"terminals.{name} must be a YAML mapping, got {type(entry).__name__}",
                code=ErrorCode.INVALID_INPUT,
            )
        args_raw = entry.get("args") or []
        env_raw = entry.get("env") or {}
        if not isinstance(args_raw, list):
            raise OmnigentError(
                f"terminals.{name}.args must be a list", code=ErrorCode.INVALID_INPUT
            )
        if not isinstance(env_raw, dict):
            raise OmnigentError(
                f"terminals.{name}.env must be a mapping", code=ErrorCode.INVALID_INPUT
            )
        # os_env may be a nested mapping (parsed like top-level os_env), the
        # literal string "inherit", or absent.
        raw_os_env = entry.get("os_env")
        os_env = raw_os_env if isinstance(raw_os_env, str) else _parse_os_env(raw_os_env)
        terminals[name] = TerminalEnvSpec(
            command=entry.get("command"),
            args=[str(a) for a in args_raw],
            env={str(k): str(v) for k, v in env_raw.items()},
            os_env=os_env,
            allow_cwd_override=bool(entry.get("allow_cwd_override", False)),
            allow_sandbox_override=bool(entry.get("allow_sandbox_override", False)),
            log_file=entry.get("log_file"),
            scrollback=int(entry.get("scrollback", 10000)),
            session_prefix=str(entry.get("session_prefix", "omni_")),
            tmux_allow_passthrough=bool(entry.get("tmux_allow_passthrough", False)),
            tmux_start_on_attach=bool(entry.get("tmux_start_on_attach", False)),
        )
    return terminals or None

def _parse_os_env_sandbox(
    raw: object,
) -> OSEnvSandboxSpec | None:
    """
    Parse the ``os_env.sandbox:`` block into an
    :class:`OSEnvSandboxSpec`.

    :param raw: The raw ``sandbox:`` value from the
        ``os_env:`` mapping. Either a mapping (parsed) or
        absent (``None``). Example:
        ``{"type": "linux_bwrap", "read_paths": ["/usr"],
        "write_paths": ["."], "write_files":
        ["/home/me/.claude.json"], "cwd_allow_hidden": [".venv",
        ".git"], "cwd_hidden_scan_max_entries": 100000,
        "cwd_hidden_scan_overflow": "warn",
        "env_passthrough": ["AWS_PROFILE", "GITHUB_TOKEN"],
        "allow_network": False}``.
    :returns: A populated :class:`OSEnvSandboxSpec` when the
        block is present, ``None`` when absent.
    :raises OmnigentError: If *raw* is not a mapping, or
        ``cwd_allow_hidden`` is not a list of strings, or any
        ``cwd_allow_hidden`` entry contains a path separator, or
        ``cwd_hidden_scan_max_entries`` is not a positive integer,
        or ``cwd_hidden_scan_overflow`` is not one of ``"error"``,
        ``"warn"``, ``"unlimited"``, or ``env_passthrough`` is not
        a list of POSIX environment variable names.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"os_env.sandbox must be a YAML mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    read_paths_raw = raw.get("read_paths")
    write_paths_raw = raw.get("write_paths")
    write_files_raw = raw.get("write_files")
    cwd_allow_hidden = _parse_cwd_allow_hidden(raw.get("cwd_allow_hidden"))
    max_entries = _parse_cwd_hidden_scan_max_entries(raw.get("cwd_hidden_scan_max_entries"))
    overflow = _parse_cwd_hidden_scan_overflow(raw.get("cwd_hidden_scan_overflow"))
    env_passthrough = _parse_env_passthrough(raw.get("env_passthrough"))
    egress_rules = _parse_egress_rules(raw.get("egress_rules"))
    raw_type = raw.get("type")
    if raw_type is None:
        # No ``type:`` field in the sandbox block -- resolve via the
        # platform default (the same logic that fires when ``sandbox:``
        # is omitted entirely). On Linux this picks ``linux_bwrap``
        # when bwrap is on PATH, else ``none``; on macOS it
        # picks ``darwin_seatbelt``.
        from omnigent.inner.sandbox import _default_sandbox_for_platform

        sandbox_type = _default_sandbox_for_platform().type
    else:
        sandbox_type = str(raw_type)
    if egress_rules and sandbox_type not in ("linux_bwrap", "darwin_seatbelt"):
        raise OmnigentError(
            "os_env.sandbox.egress_rules requires sandbox.type=linux_bwrap "
            "(Linux) or sandbox.type=darwin_seatbelt (macOS) for hard "
            "network enforcement: those backends restrict network access "
            "at spawn time so the MITM proxy is the only egress path. "
            f"Got sandbox.type={sandbox_type!r}.",
            code=ErrorCode.INVALID_INPUT,
        )
    credential_proxy = _parse_credential_proxy(raw.get("credential_proxy"))
    if credential_proxy is not None and sandbox_type not in ("linux_bwrap", "darwin_seatbelt"):
        raise OmnigentError(
            "os_env.sandbox.credential_proxy requires sandbox.type=linux_bwrap "
            "(Linux) or sandbox.type=darwin_seatbelt (macOS) so credentials are "
            "bound to a hardened helper boundary. "
            f"Got sandbox.type={sandbox_type!r}.",
            code=ErrorCode.INVALID_INPUT,
        )
    if credential_proxy is not None and not egress_rules:
        raise OmnigentError(
            "os_env.sandbox.credential_proxy requires os_env.sandbox.egress_rules: "
            "the MITM egress proxy is what swaps the synthetic placeholder for the "
            "real credential and rejects placeholder leaks, so it must be active.",
            code=ErrorCode.INVALID_INPUT,
        )
    macos_reason = _credential_proxy_macos_unsupported_reason(credential_proxy, sandbox_type)
    if macos_reason is not None:
        raise OmnigentError(macos_reason, code=ErrorCode.INVALID_INPUT)
    allow_private = raw.get("egress_allow_private_destinations", False)
    if not isinstance(allow_private, bool):
        raise OmnigentError(
            "os_env.sandbox.egress_allow_private_destinations must be a "
            f"boolean, got {type(allow_private).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return OSEnvSandboxSpec(
        type=sandbox_type,
        read_paths=[str(p) for p in read_paths_raw] if read_paths_raw is not None else None,
        write_paths=[str(p) for p in write_paths_raw] if write_paths_raw is not None else None,
        write_files=[str(p) for p in write_files_raw] if write_files_raw is not None else None,
        allow_network=bool(raw.get("allow_network", True)),
        cwd_allow_hidden=cwd_allow_hidden,
        cwd_hidden_scan_max_entries=max_entries,
        cwd_hidden_scan_overflow=overflow,
        env_passthrough=env_passthrough,
        egress_rules=egress_rules,
        egress_allow_private_destinations=allow_private,
        credential_proxy=credential_proxy,
    )

def _parse_cwd_allow_hidden(raw: object) -> list[str] | None:
    """
    Parse and validate the ``cwd_allow_hidden:`` field of
    ``os_env.sandbox``.

    Each entry must be a single path component (no ``/``, ``\\``,
    or ``.`` / ``..`` traversal) so a misconfigured spec can't punch
    a hole through arbitrary subdirectories of cwd. The bwrap backend
    looks each entry up in ``cwd.iterdir()`` directly; sanitising
    here keeps the resolver simple and the failure mode loud at
    parse time rather than at runtime.

    :param raw: Raw value from the YAML, e.g. ``[".venv", ".git"]``,
        or ``None`` when the field is absent.
    :returns: List of validated component names, or ``None`` when
        ``raw`` is ``None`` (the resolver will then apply the
        backend's documented default).
    :raises OmnigentError: If ``raw`` isn't a list, contains a
        non-string entry, or contains an entry with a path separator
        or traversal component.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"os_env.sandbox.cwd_allow_hidden must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    sanitized: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise OmnigentError(
                "os_env.sandbox.cwd_allow_hidden entries must be strings, "
                f"got {type(entry).__name__}: {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        if not entry:
            raise OmnigentError(
                "os_env.sandbox.cwd_allow_hidden entries must not be empty strings",
                code=ErrorCode.INVALID_INPUT,
            )
        if "/" in entry or "\\" in entry or entry in (".", ".."):
            raise OmnigentError(
                "os_env.sandbox.cwd_allow_hidden entries must be single path "
                f"components (no separators or '.'/'..'): {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        sanitized.append(entry)
    return sanitized

def _parse_cwd_hidden_scan_max_entries(raw: object) -> int:
    """
    Parse ``os_env.sandbox.cwd_hidden_scan_max_entries``.

    Falls back to the dataclass default (50000) when the field is
    absent. Rejects non-integers and non-positive values at parse
    time so a misconfiguration surfaces immediately rather than at
    spawn time.

    YAML readers occasionally hand us ``True`` / ``False`` for
    fields the author meant as numbers; the explicit ``bool``
    rejection below catches that.

    :param raw: Raw value from the YAML, e.g. ``100000`` or ``None``.
    :returns: Validated positive integer, or the dataclass default
        when ``raw`` is ``None``.
    :raises OmnigentError: If ``raw`` is not an int or is not
        strictly positive.
    """
    if raw is None:
        return OSEnvSandboxSpec.__dataclass_fields__["cwd_hidden_scan_max_entries"].default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise OmnigentError(
            "os_env.sandbox.cwd_hidden_scan_max_entries must be an integer, "
            f"got {type(raw).__name__}: {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    if raw <= 0:
        raise OmnigentError(
            f"os_env.sandbox.cwd_hidden_scan_max_entries must be > 0, got {raw}",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw

def _parse_cwd_hidden_scan_overflow(raw: object) -> str:
    """
    Parse ``os_env.sandbox.cwd_hidden_scan_overflow``.

    Falls back to the dataclass default (``"warn"``) when the field
    is absent — a partial best-effort mask plus a ``CRITICAL`` log
    line, which beats blocking every spawn on workspaces (notably
    ones with ``node_modules``) that routinely exceed the cap. Set
    ``"error"`` explicitly for untrusted trees. Rejects any value not
    in :data:`_CWD_HIDDEN_SCAN_OVERFLOW_MODES`.

    :param raw: Raw value from the YAML, e.g. ``"warn"`` or ``None``.
    :returns: One of ``"error"``, ``"warn"``, ``"unlimited"``.
    :raises OmnigentError: If ``raw`` is not one of the supported
        modes.
    """
    if raw is None:
        return OSEnvSandboxSpec.__dataclass_fields__["cwd_hidden_scan_overflow"].default
    if not isinstance(raw, str) or raw not in _CWD_HIDDEN_SCAN_OVERFLOW_MODES:
        raise OmnigentError(
            "os_env.sandbox.cwd_hidden_scan_overflow must be one of "
            f"{list(_CWD_HIDDEN_SCAN_OVERFLOW_MODES)}, got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw

def _parse_env_passthrough(raw: object) -> list[str] | None:
    """
    Parse and validate the ``env_passthrough:`` field of
    ``os_env.sandbox``.

    Each entry must be a syntactically valid POSIX environment
    variable name (``[A-Za-z_][A-Za-z0-9_]*``) so we can pass it
    straight to ``os.execve`` and so that an entry containing ``=``
    or other shell-meaningful characters can't smuggle a *value*
    through the *name* slot.

    Validation happens here at parse time so a misconfigured spec
    fails immediately rather than silently passing through a bogus
    name (which would be a no-op at spawn time and silently
    weaken whatever the user thought they were granting).

    :param raw: Raw value from the YAML, e.g.
        ``["AWS_PROFILE", "GITHUB_TOKEN"]``, or ``None`` when the
        field is absent.
    :returns: List of validated env-var names, or ``None`` when
        ``raw`` is ``None`` (the helper will then inherit only the
        always-passed defaults).
    :raises OmnigentError: If ``raw`` isn't a list, contains a
        non-string entry, or contains an entry that isn't a valid
        POSIX env var name.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"os_env.sandbox.env_passthrough must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    sanitized: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise OmnigentError(
                "os_env.sandbox.env_passthrough entries must be strings, "
                f"got {type(entry).__name__}: {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        if not _ENV_VAR_NAME_RE.match(entry):
            raise OmnigentError(
                "os_env.sandbox.env_passthrough entries must be POSIX "
                "environment variable names "
                f"(letters/digits/underscore, not starting with a digit): {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        sanitized.append(entry)
    return sanitized

def _parse_egress_rules(raw: object) -> list[str] | None:
    """
    Parse and validate the ``egress_rules:`` field of
    ``os_env.sandbox``.

    Each entry is validated at parse time via
    :func:`~omnigent.inner.egress.rules.parse_rule` so syntax
    errors surface immediately rather than at proxy start time.

    :param raw: The raw value from the YAML mapping. ``None``
        means "no egress filtering".
    :returns: A list of validated rule strings, or ``None``.
    :raises OmnigentError: If the value isn't a list or any
        rule fails to parse.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"os_env.sandbox.egress_rules must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not raw:
        return None
    from omnigent.inner.egress.rules import parse_rule

    validated: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, str):
            raise OmnigentError(
                "os_env.sandbox.egress_rules entries must be strings, "
                f"got {type(entry).__name__} at index {i}: {entry!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            parse_rule(entry)
        except ValueError as exc:
            raise OmnigentError(
                f"os_env.sandbox.egress_rules[{i}] is invalid: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        validated.append(entry)
    return validated


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _capabilities as _sib_capabilities
    from . import _core as _sib_core
    from . import _credentials as _sib_credentials
    from . import _discover as _sib_discover
    from . import _guardrails as _sib_guardrails
    from . import _helpers as _sib_helpers
    from . import _llm as _sib_llm
    from . import _mcp as _sib_mcp
    from . import _policies as _sib_policies
    from . import _skills as _sib_skills
    from . import _tools as _sib_tools
    for _key, _value in _sib_capabilities.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_core.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_credentials.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_discover.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_guardrails.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_llm.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_mcp.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_policies.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_skills.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_tools.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
