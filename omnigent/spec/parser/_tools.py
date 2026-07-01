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

def _parse_tools_config(
    raw: dict[str, Any] | None,
) -> ToolsConfig:
    """
    Parse the ``tools:`` block from config.yaml into a
    :class:`ToolsConfig`.

    :param raw: The raw ``tools:`` mapping from config.yaml, or
        ``None`` if the block was absent. Example:
        ``{"agents": ["summarizer", "code-reviewer"],
        "timeout": 60}``.
    :returns: A populated :class:`ToolsConfig`. Returns defaults
        when *raw* is ``None``.
    """
    if raw is None:
        return ToolsConfig()
    timeout = int(raw["timeout"]) if "timeout" in raw else 60
    retry = _parse_retry(raw.get("retry"))
    builtins = _parse_builtin_tools(raw.get("builtins", []))
    sandbox = _parse_sandbox_config(raw.get("sandbox"))
    return ToolsConfig(
        agents=raw.get("agents", []),
        builtins=builtins,
        timeout=timeout,
        retry=retry,
        sandbox=sandbox,
    )

def _parse_sandbox_config(
    raw: dict[str, Any] | None,
) -> SandboxConfig:
    """
    Parse the ``tools.sandbox`` block from config.yaml.

    Only agent-level settings (``docker_image``) are parsed here.
    Whether sandboxing is enabled is a runtime decision, not an
    agent config decision::

        sandbox:
          docker_image: python:3.12-slim

    :param raw: The raw ``sandbox`` value from the ``tools``
        block. ``None`` means not specified (use defaults).
    :returns: A :class:`SandboxConfig`.
    """
    if raw is None or not isinstance(raw, dict):
        return SandboxConfig()
    return SandboxConfig(
        docker_image=raw.get("docker_image"),
    )

def _parse_builtin_tools(
    raw: list[str | dict[str, Any]],
) -> list[BuiltinToolConfig]:
    """
    Parse the ``tools.builtins`` list into
    :class:`BuiltinToolConfig` objects.

    Each entry is either a plain string (tool name with no config)
    or a dict with a ``name`` key and tool-specific config fields::

        builtins:
          - web_search
          - name: web_search
            api_key: ${GOOGLE_SEARCH_API_KEY}
            engine_id: ${GOOGLE_SEARCH_ENGINE_ID}

    :param raw: The raw ``builtins`` list from config.yaml.
    :returns: A list of :class:`BuiltinToolConfig` instances.
    :raises OmnigentError: If a dict entry is missing ``name``.
    """
    result: list[BuiltinToolConfig] = []
    for entry in raw:
        if isinstance(entry, str):
            result.append(BuiltinToolConfig(name=entry))
        elif isinstance(entry, dict):
            name = entry.get("name")
            if not name:
                raise OmnigentError(
                    "Each dict entry in tools.builtins must have a 'name' field.",
                    code=ErrorCode.INVALID_INPUT,
                )
            # Everything except 'name' is tool-specific config.
            config = {str(k): str(v) for k, v in entry.items() if k != "name"}
            result.append(
                BuiltinToolConfig(
                    name=str(name),
                    config=config,
                )
            )
        else:
            raise OmnigentError(
                f"tools.builtins entries must be strings or dicts, got {type(entry).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
    return result

def _parse_retry(
    raw: dict[str, Any] | None,
) -> RetryPolicy:
    """
    Parse a ``retry:`` block into a :class:`RetryPolicy`.

    Returns defaults when *raw* is ``None`` or empty.

    :param raw: The raw ``retry:`` mapping, or ``None`` if absent.
        Example: ``{"max_attempts": 5, "status_codes": [429, 502]}``.
    :returns: A populated :class:`RetryPolicy`.
    """
    if not raw:
        return RetryPolicy()
    defaults = RetryPolicy()
    return RetryPolicy(
        max_retries=int(raw.get("max_retries", defaults.max_retries)),
        backoff_base_s=float(raw.get("backoff_base_s", defaults.backoff_base_s)),
        backoff_max_s=float(raw.get("backoff_max_s", defaults.backoff_max_s)),
        jitter=bool(raw.get("jitter", defaults.jitter)),
        timeout_per_request_s=(
            float(raw["timeout_per_request_s"])
            if raw.get("timeout_per_request_s") is not None
            else defaults.timeout_per_request_s
        ),
        retryable_status_codes=tuple(
            int(c) for c in raw.get("retryable_status_codes", defaults.retryable_status_codes)
        ),
    )

def _parse_executor(
    raw: dict[str, Any] | None,
    *,
    raw_tools: object = None,
    expand_env: bool = True,
) -> ExecutorSpec:
    """
    Parse the ``executor:`` block into an :class:`ExecutorSpec`.

    Returns defaults (``type="omnigent"``) when *raw* is ``None``.

    Lifts a top-level ``executor.profile`` into the concrete
    :attr:`ExecutorSpec.profile` field for ALL executor types. For
    ``type == "omnigent"`` ALSO mirrors that value into
    ``config["profile"]`` (back-compat — the omnigent executor
    reads ``config["profile"]`` today; will be migrated when the
    omnigent-compat sunset lands). When
    ``config["harness"] == "databricks_supervisor"``, parses the *raw_tools*
    list of typed tool dicts into
    :attr:`ExecutorSpec.supervisor_tools`.

    :param raw: The raw ``executor:`` mapping, or ``None`` if
        absent. Example: ``{"type": "omnigent"}``.
    :param raw_tools: The raw top-level ``tools:`` value from
        config.yaml. Routed into
        :attr:`ExecutorSpec.supervisor_tools` only when
        ``config["harness"] == "databricks_supervisor"``; ignored otherwise.
        Passed in (rather than read from *raw*) because the
        supervisor's ``tools`` live at the YAML top level
        alongside ``executor:``, not nested inside it.
    :returns: A populated :class:`ExecutorSpec`.
    :raises OmnigentError: If the supervisor harness is selected
        and the ``tools:`` list is malformed (see
        :func:`_parse_supervisor_tools`).
    """
    if raw is None:
        return ExecutorSpec()
    etype = str(raw.get("type", "omnigent"))
    # ``config`` is a free-form dict[str, Any] owned by each executor
    # type. Scalar values are coerced to strings so YAML booleans /
    # numbers round-trip as their string form (the omnigent
    # harness/profile fields are both strings in the source YAML).
    # Structured keys whose consumer needs the nested shape are kept
    # verbatim: ``cost_optimize`` is the cost advisor's tier config (a
    # nested mapping), which ``parse_advisor_config`` reads as a Mapping.
    raw_config = raw.get("config")
    config: dict[str, Any] = {}
    if isinstance(raw_config, dict):
        config = {
            str(k): (v if k in _STRUCTURED_EXECUTOR_CONFIG_KEYS else str(v))
            for k, v in raw_config.items()
        }
    # Top-level ``executor.profile`` populates the concrete
    # ``ExecutorSpec.profile`` field for every executor type. For
    # ``omnigent`` we ALSO mirror it into ``config["profile"]``
    # so the existing omnigent executor (which still reads from
    # ``config["profile"]``) keeps working until it is migrated.
    profile_raw = raw.get("profile")
    profile: str | None = None
    if profile_raw is not None:
        profile = str(profile_raw)
    if etype == "omnigent" and profile is not None and "profile" not in config:
        config["profile"] = profile
    is_supervisor = config.get("harness") == "databricks_supervisor"
    supervisor_tools = _parse_supervisor_tools(
        raw_tools, is_supervisor=is_supervisor, expand_env=expand_env
    )
    raw_cw = raw.get("context_window")
    context_window: int | None = int(raw_cw) if raw_cw is not None else None
    raw_model = raw.get("model")
    model: str | None = str(raw_model) if raw_model is not None else None
    # Parse ``executor.connection:`` — same shape as ``llm.connection:``
    # (a flat dict of string key-value pairs with optional ${VAR}
    # expansion). Lifted from the ``executor:`` block so connection
    # config lives alongside the harness and model it belongs to.
    connection_raw = raw.get("connection")
    connection: dict[str, str] | None = None
    if isinstance(connection_raw, dict):
        raw_dict = {str(k): str(v) for k, v in connection_raw.items()}
        connection = expand_env_vars(raw_dict) if expand_env else raw_dict
    auth = _parse_executor_auth(raw, expand_env=expand_env)
    return ExecutorSpec(
        type=etype,
        timeout=int(raw.get("timeout", 3600)),
        max_iterations=int(raw.get("max_iterations", 1000)),
        profile=profile,
        config=config,
        model=model,
        connection=connection,
        context_window=context_window,
        supervisor_tools=supervisor_tools,
        auth=auth,
    )

def _parse_blueprint(raw: object) -> BlueprintSpec | None:
    """
    Parse the top-level ``blueprint:`` block.

    :param raw: Raw YAML value from ``config.yaml``.
    :returns: Parsed :class:`BlueprintSpec`, or ``None`` when absent.
    :raises OmnigentError: If the block is structurally invalid.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            "blueprint must be a mapping",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_nodes = raw.get("nodes", [])
    if raw_nodes is None:
        raw_nodes = []
    if not isinstance(raw_nodes, list):
        raise OmnigentError(
            "blueprint.nodes must be a list",
            code=ErrorCode.INVALID_INPUT,
        )
    outputs = raw.get("outputs", {})
    if outputs is None:
        outputs = {}
    if not isinstance(outputs, dict):
        raise OmnigentError(
            "blueprint.outputs must be a mapping",
            code=ErrorCode.INVALID_INPUT,
        )
    version = raw.get("version", 1)
    return BlueprintSpec(
        name=str(raw["name"]) if raw.get("name") is not None else None,
        description=str(raw["description"]) if raw.get("description") is not None else None,
        nodes=[
            _parse_blueprint_node(node_raw, f"blueprint.nodes[{idx}]")
            for idx, node_raw in enumerate(raw_nodes)
        ],
        outputs=dict(outputs),
        version=int(version),
    )

def _parse_blueprint_node(raw: object, path: str) -> BlueprintNode:
    """
    Parse one blueprint node mapping.

    :param raw: Raw YAML node.
    :param path: Human-readable path for error messages.
    :returns: Parsed :class:`BlueprintNode`.
    :raises OmnigentError: If the node is structurally invalid.
    """
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"{path} must be a mapping",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_id = raw.get("id")
    raw_kind = raw.get("kind")
    if raw_id is None:
        raise OmnigentError(
            f"{path}.id is required",
            code=ErrorCode.INVALID_INPUT,
        )
    if raw_kind is None:
        raise OmnigentError(
            f"{path}.kind is required",
            code=ErrorCode.INVALID_INPUT,
        )
    metadata: dict[str, Any] = {}
    raw_metadata = raw.get("metadata")
    if isinstance(raw_metadata, dict):
        metadata.update(raw_metadata)
    metadata.update(
        {str(key): value for key, value in raw.items() if key not in _BLUEPRINT_NODE_RESERVED_KEYS}
    )
    return BlueprintNode(
        id=str(raw_id),
        kind=str(raw_kind),  # type: ignore[arg-type]
        depends_on=_parse_blueprint_depends(raw),
        when=raw.get("when"),
        target=str(raw["target"]) if raw.get("target") is not None else None,
        input=raw.get("input"),
        return_mapping=raw.get("return"),
        output=raw.get("output"),
        loop=_parse_blueprint_loop(raw, path),
        metadata=metadata,
    )

def _parse_blueprint_depends(raw: dict[str, Any]) -> list[str]:
    """Parse ``depends_on`` / ``depends`` into a list of node ids."""
    raw_depends = raw.get("depends_on", raw.get("depends", []))
    if raw_depends is None:
        return []
    if isinstance(raw_depends, str):
        return [raw_depends]
    if isinstance(raw_depends, list):
        return [str(item) for item in raw_depends]
    raise OmnigentError(
        "blueprint node depends_on must be a string or list",
        code=ErrorCode.INVALID_INPUT,
    )

def _parse_blueprint_loop(raw: dict[str, Any], path: str) -> BlueprintLoopSpec | None:
    """Parse ``loop:`` config for a blueprint node, if present."""
    raw_loop = raw.get("loop")
    if raw_loop is None:
        return None
    if not isinstance(raw_loop, dict):
        raise OmnigentError(
            f"{path}.loop must be a mapping",
            code=ErrorCode.INVALID_INPUT,
        )
    body = raw_loop.get("body", [])
    if body is None:
        body = []
    if not isinstance(body, list):
        raise OmnigentError(
            f"{path}.loop.body must be a list",
            code=ErrorCode.INVALID_INPUT,
        )
    return BlueprintLoopSpec(
        max_iterations=int(raw_loop.get("max_iterations", 0)),
        until=raw_loop.get("until"),
        on_exhausted=str(raw_loop.get("on_exhausted", "fail")),  # type: ignore[arg-type]
        body=[
            _parse_blueprint_node(node_raw, f"{path}.loop.body[{idx}]")
            for idx, node_raw in enumerate(body)
        ],
        reuse_session=bool(raw_loop.get("reuse_session", False)),
    )

def _parse_executor_auth(
    raw: dict[str, Any],  # type: ignore[explicit-any]
    *,
    expand_env: bool = True,
) -> ApiKeyAuth | DatabricksAuth | ProviderAuth | None:
    """
    Parse the ``executor.auth:`` block into a typed auth dataclass.

    Returns ``None`` when the ``auth:`` key is absent from the executor
    block (the harness will fall back to env-var / profile defaults).

    Supported types:

    - ``type: api_key`` — requires ``api_key``.  Env-var references
      (e.g. ``$OPENAI_API_KEY``) are expanded when *expand_env* is
      ``True``.
    - ``type: databricks`` — requires ``profile``.
    - ``type: provider`` — requires ``name`` (a provider declared in
      the ``providers:`` block of ``~/.omnigent/config.yaml``).

    :param raw: The raw ``executor:`` mapping already read from YAML.
        Example: ``{"harness": "openai-agents", "auth": {"type": "api_key",
        "api_key": "$OPENAI_API_KEY"}}``.
    :param expand_env: Whether to expand ``${VAR}`` / ``$VAR`` references
        in the ``api_key`` value. ``True`` for runtime; ``False`` for
        scaffolding / validation where env vars may not be set yet.
    :returns: A populated :class:`ApiKeyAuth`, :class:`DatabricksAuth`,
        or :class:`ProviderAuth`, or ``None`` when ``auth:`` is absent.
    :raises OmnigentError: If the ``auth:`` block is present but
        malformed (unknown type, missing required field).
    """
    raw_auth = raw.get("auth")
    if raw_auth is None:
        return None
    if not isinstance(raw_auth, dict):
        raise OmnigentError(
            "executor.auth must be a mapping, e.g. {type: databricks, profile: oss}",
            code=ErrorCode.INVALID_INPUT,
        )
    auth_type = str(raw_auth.get("type", ""))
    if auth_type == "api_key":
        raw_key = str(raw_auth.get("api_key") or "")
        if not raw_key:
            raise OmnigentError(
                "executor.auth.api_key is required when type is 'api_key'",
                code=ErrorCode.INVALID_INPUT,
            )
        api_key = expand_env_vars({"api_key": raw_key})["api_key"] if expand_env else raw_key
        raw_base_url = raw_auth.get("base_url")
        base_url: str | None = None
        if raw_base_url is not None:
            raw_base_url_str = str(raw_base_url)
            base_url = (
                expand_env_vars({"base_url": raw_base_url_str})["base_url"]
                if expand_env
                else raw_base_url_str
            )
        return ApiKeyAuth(api_key=api_key, base_url=base_url)
    if auth_type == "databricks":
        profile_val = str(raw_auth.get("profile") or "")
        if not profile_val:
            raise OmnigentError(
                "executor.auth.profile is required when type is 'databricks'",
                code=ErrorCode.INVALID_INPUT,
            )
        return DatabricksAuth(profile=profile_val)
    if auth_type == "provider":
        name_val = str(raw_auth.get("name") or "")
        if not name_val:
            raise OmnigentError(
                "executor.auth.name is required when type is 'provider'",
                code=ErrorCode.INVALID_INPUT,
            )
        return ProviderAuth(name=name_val)
    raise OmnigentError(
        f"executor.auth.type must be 'api_key', 'databricks', or 'provider', got {auth_type!r}",
        code=ErrorCode.INVALID_INPUT,
    )

def _parse_supervisor_tools(  # type: ignore[explicit-any]
    raw_tools: object,
    *,
    is_supervisor: bool,
    expand_env: bool = True,
) -> list[dict[str, Any]] | None:
    """
    Parse the top-level ``tools:`` list for the supervisor harness.

    Returns ``None`` when *is_supervisor* is ``False``. When
    ``True``, validates that every entry uses the Databricks
    Supervisor API's nested shape
    (``{"type": X, X: {<config>}}``), expands ``${VAR}`` references
    inside the nested sub-dict against the current environment
    (when *expand_env* is True), and checks that all per-type
    required fields are present. Each entry round-trips verbatim so
    the supervisor executor can forward the list to the gateway
    with no reshaping.

    :param raw_tools: The raw top-level ``tools:`` value from
        config.yaml. ``None`` is treated as an empty list when
        *is_supervisor* is ``True``. Example:
        ``[{"type": "genie_space",
        "genie_space": {"id": "${GENIE_SPACE_ID}", "description": "..."}}]``.
    :param is_supervisor: Whether the supervisor harness is
        selected (``config["harness"] == "databricks_supervisor"``).
    :param expand_env: When ``True`` (default), expand ``${VAR}``
        / ``$VAR`` references in nested string values via
        :func:`expand_env_vars` and fail loud on unresolved refs.
        When ``False`` (scaffolding / validation paths), skip
        expansion — the caller is not running the agent for real.
    :returns: A list of verbatim nested tool dicts when
        *is_supervisor* is ``True`` (possibly empty);
        ``None`` otherwise.
    :raises OmnigentError: If *raw_tools* is not a list, an
        entry is not a dict, an entry's ``type`` is missing or
        not in the supported set, the nested ``<type>:`` sub-dict
        is missing, a required field is missing, or (when
        *expand_env* is True) a ``${VAR}`` reference cannot be
        resolved against the environment.
    """
    if not is_supervisor:
        return None
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list):
        raise OmnigentError(
            "tools must be a YAML list when the supervisor harness "
            f"is selected, got {type(raw_tools).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return [
        _validate_supervisor_tool_entry(index, entry, expand_env=expand_env)
        for index, entry in enumerate(raw_tools)
    ]


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
    from . import _os_env as _sib_os_env
    from . import _policies as _sib_policies
    from . import _skills as _sib_skills
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
    for _key, _value in _sib_os_env.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_policies.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_skills.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
