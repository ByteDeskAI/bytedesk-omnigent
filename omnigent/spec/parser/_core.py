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

class _ConfigYamlLoader(yaml.SafeLoader):
    """
    SafeLoader variant that does NOT treat ``on``/``off``/
    ``yes``/``no`` as booleans.

    Default PyYAML resolves these per the YAML 1.1 spec — a
    trap for our spec because the policy system uses
    ``on:`` as the selector field (see POLICIES.md §3.3
    implementation notes). Without this override, an author
    writing ``on: [request]`` would get a dict keyed by ``True``
    instead of ``"on"``. We scope the override to a dedicated
    loader class so the rest of the YAML 1.1 type inference
    stays intact.

    YAML 1.2 drops these bool aliases entirely; this override
    makes our loader YAML-1.2-aligned for the narrow set of
    aliases that matter here.
    """

def parse(root: Path, *, expand_env: bool = True) -> AgentSpec:
    """
    Parse an agent image directory into an :class:`AgentSpec`.

    :param root: Path to the agent image directory. Must contain
        ``config.yaml``.
    :param expand_env: Whether to expand ``${VAR}`` references in
        connection blocks and MCP URLs/headers. ``True`` (default)
        for deploy/runtime — raises on unresolved vars. ``False``
        for scaffolding/validation where env vars may not yet be set.
    :returns: A fully populated :class:`AgentSpec` (not yet
        validated).
    :raises OmnigentError: If ``config.yaml`` is not valid YAML,
        has structural issues, or (when *expand_env* is ``True``)
        contains unresolved env vars.
    :raises FileNotFoundError: If ``config.yaml`` is missing.
    """
    config_path = root / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found in {root}")

    raw = yaml.load(config_path.read_text(), Loader=_ConfigYamlLoader)
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"config.yaml must be a YAML mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )

    spec_version = raw.get("spec_version")
    if spec_version is None:
        raise OmnigentError(
            "config.yaml missing required field: spec_version",
            code=ErrorCode.INVALID_INPUT,
        )

    # Determine the executor type and (for omnigent) the harness
    # up front so the rest of the parse can route type-specific
    # YAML shapes correctly. The supervisor harness expects
    # ``tools:`` as a top-level list of typed dicts (rejected by
    # the legacy ``_parse_tools_config`` which expects a mapping).
    raw_executor = raw.get("executor")
    executor_type = "omnigent"
    if isinstance(raw_executor, dict) and raw_executor.get("type") is not None:
        executor_type = str(raw_executor["type"])
    # Peek at executor.config.harness to detect the supervisor
    # harness before _parse_executor runs. Needed because the
    # tools-list parse path must branch on this value.
    _raw_cfg = raw_executor.get("config", {}) if isinstance(raw_executor, dict) else {}
    _is_supervisor_harness = (
        executor_type == "omnigent"
        and isinstance(_raw_cfg, dict)
        and str(_raw_cfg.get("harness", "")) == "databricks_supervisor"
    )
    raw_llm = raw.get("llm")
    raw_tools = raw.get("tools")
    llm = _parse_llm(raw_llm, expand_env=expand_env)
    interaction = _parse_interaction(raw.get("interaction"))
    # When the supervisor harness is selected, the top-level
    # ``tools:`` is a list of typed dicts that lands in
    # ``ExecutorSpec.supervisor_tools`` (verbatim). Skip the
    # legacy ToolsConfig path — it expects a mapping and would
    # otherwise reject a list with a confusing error.
    if _is_supervisor_harness:
        tools_config = ToolsConfig()
    else:
        tools_config = _parse_tools_config(raw_tools)
    executor = _parse_executor(raw_executor, raw_tools=raw_tools, expand_env=expand_env)
    # ── Consolidate llm: → executor ────────────────────────────────
    # ``executor.model`` and ``executor.connection`` are the primary
    # source of truth. When the deprecated ``llm:`` block provides
    # values that the ``executor:`` block doesn't, lift them into
    # executor so all downstream code reads from one place.
    # ``spec.llm`` is still populated (for internal consumers that
    # need extra/retry/request_timeout) but model and connection
    # are authoritative on executor.
    if llm is not None:
        if executor.model is None:
            executor.model = llm.model
        if executor.connection is None and llm.connection is not None:
            executor.connection = llm.connection
    # Ensure spec.llm is populated from executor fields when only the
    # executor: block declares model/connection (the common case for
    # user-authored YAML). Internal consumers (policy builder,
    # web_fetch sub-agent) still read spec.llm for extra, retry,
    # and request_timeout.
    if llm is None and executor.model is not None:
        llm = LLMConfig(model=executor.model, connection=executor.connection)
    elif llm is not None:
        # Keep llm.model and llm.connection in sync with executor
        # (executor is authoritative after the lift above).
        llm = LLMConfig(
            model=executor.model or llm.model,
            extra=llm.extra,
            connection=executor.connection,
            request_timeout=llm.request_timeout,
            retry=llm.retry,
        )
    compaction = _parse_compaction(raw.get("compaction"))
    guardrails = _parse_guardrails(raw.get("guardrails"), expand_env=expand_env)
    os_env = _parse_os_env(raw.get("os_env"))
    terminals = _parse_terminals(raw.get("terminals"))
    params = raw.get("params", {})
    # Top-level ``async:`` flag gates the LLM-callable async-dispatch
    # builtins (``sys_call_async``, ``sys_read_inbox``,
    # ``sys_cancel_async``). Defaults to True to match
    # ``omnigent/inner/datamodel.py::AgentDef.async_enabled`` — the
    # same YAML must produce the same tool surface under Omnigent mode and
    # the legacy inner stack. Agents that want to suppress the surface
    # declare ``async: false`` explicitly. ``bool()`` accepts YAML
    # truthy/falsy values (``true`` / ``True`` / ``yes`` /
    # ``false`` / ``no``) consistently.
    async_enabled = bool(raw.get("async", True))
    # Top-level ``timers:`` flag gates the LLM-callable timer
    # builtins (``sys_timer_set``, ``sys_timer_cancel``).
    # Defaults to False to match
    # ``omnigent/inner/datamodel.py::AgentDef.timers`` — agents
    # opt into the timer surface explicitly. See step 10 of the
    # harness contract migration.
    timers = bool(raw.get("timers", False))
    # Top-level ``spawn:`` flag grants spawning OUTSIDE any declared
    # sub-agent list: ``sys_session_create`` (existing agents by id,
    # or custom bundles via config_path) plus send/close to drive the
    # children. Distinct from ``tools.agents``, which permits only
    # the specified sub-agent types. Defaults to False — session
    # reads stay always-on, but every write grant is explicit.
    spawn = bool(raw.get("spawn", False))
    # Top-level ``capabilities:`` list — first-class capability surface
    # (BDP-2334). Parsed into an immutable tuple of non-empty slugs;
    # absent => empty tuple. Consumed by the capability resolver.
    capabilities = _parse_capabilities(raw.get("capabilities"))
    # Top-level ``output_schema:`` — structured-output contract (BDP-2393).
    # A JSON Schema mapping; ignored unless it is a dict (free-text default).
    raw_output_schema = raw.get("output_schema")
    output_schema = raw_output_schema if isinstance(raw_output_schema, dict) else None
    blueprint = _parse_blueprint(raw.get("blueprint"))

    # Honor ``prompt:`` as the legacy alias for ``instructions:`` (per
    # ``_OMNIGENT_SYSTEM_PROMPT_KEYS``); ``instructions:`` wins if both set.
    raw_instructions = raw.get("instructions")
    if raw_instructions is None:
        raw_instructions = raw.get("prompt")
    instructions = _resolve_instructions(root, raw_instructions)
    skills = _discover_skills(root / "skills")
    skills_filter = _parse_skills_filter(raw.get("skills"))
    mcp_servers = _discover_mcp_servers(root / "tools" / "mcp", expand_env=expand_env)
    mcp_servers = mcp_servers + _parse_inline_mcp_servers(raw_tools, expand_env=expand_env)
    local_tools = _discover_local_tools(root / "tools")
    sub_agents = _discover_sub_agents(root / "agents", expand_env=expand_env)

    return AgentSpec(
        spec_version=spec_version,
        name=raw.get("name"),
        description=raw.get("description"),
        llm=llm,
        interaction=interaction,
        tools=tools_config,
        executor=executor,
        compaction=compaction,
        guardrails=guardrails,
        params=params,
        instructions=instructions,
        skills=skills,
        skills_filter=skills_filter,
        mcp_servers=mcp_servers,
        local_tools=local_tools,
        sub_agents=sub_agents,
        async_enabled=async_enabled,
        blueprint=blueprint,
        os_env=os_env,
        terminals=terminals,
        timers=timers,
        spawn=spawn,
        capabilities=capabilities,
        output_schema=output_schema,
    )

def expand_env_vars(
    mapping: dict[str, str],
) -> dict[str, str]:
    """
    Expand ``${VAR}`` and ``$VAR`` references in dict values
    against the current process environment.

    Raises :class:`OmnigentError` if any value still contains an
    unresolved ``$VAR`` or ``${VAR}`` reference after expansion.
    This catches typos and missing environment variables at parse
    time rather than silently passing literal ``${MISSING}`` to
    MCP servers or LLM clients.

    :param mapping: A string-to-string dict, e.g.
        ``{"TOKEN": "${GITHUB_TOKEN}"}``.
    :returns: A new dict with expanded values.
    :raises OmnigentError: If a value contains an unresolved
        environment variable reference after expansion.
    """
    result: dict[str, str] = {}
    for key, value in mapping.items():
        expanded = os.path.expandvars(value)
        check_unresolved_env_vars(key, expanded)
        result[key] = expanded
    return result

def check_unresolved_env_vars(key: str, value: str) -> None:
    """
    Raise if *value* contains unresolved environment variable
    references.

    Called after :func:`os.path.expandvars` to catch variables
    that were not set in the environment. Without this check,
    ``os.path.expandvars`` silently passes through the literal
    ``${VAR}`` string, which causes hard-to-debug failures
    downstream (e.g. an MCP server receiving ``$GITHUB_TOKEN``
    as a literal auth token).

    :param key: The dict key (for error messages), e.g.
        ``"GITHUB_TOKEN"``.
    :param value: The expanded value to check, e.g.
        ``"Bearer ${MISSING}"``.
    :raises OmnigentError: If *value* contains an unresolved
        ``$VAR`` or ``${VAR}`` reference.
    """
    match = _UNRESOLVED_VAR_RE.search(value)
    if match is not None:
        raise OmnigentError(
            f"Unresolved environment variable {match.group()!r} "
            f"in config key {key!r}. Set the variable in the "
            f"environment or remove the reference.",
            code=ErrorCode.INVALID_INPUT,
        )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _capabilities as _sib_capabilities
    from . import _credentials as _sib_credentials
    from . import _discover as _sib_discover
    from . import _guardrails as _sib_guardrails
    from . import _helpers as _sib_helpers
    from . import _llm as _sib_llm
    from . import _mcp as _sib_mcp
    from . import _os_env as _sib_os_env
    from . import _policies as _sib_policies
    from . import _skills as _sib_skills
    from . import _tools as _sib_tools
    for _key, _value in _sib_capabilities.__dict__.items():
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
    for _key, _value in _sib_os_env.__dict__.items():
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
