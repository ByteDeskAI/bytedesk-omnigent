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

def _parse_llm(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> LLMConfig | None:
    """
    Parse the ``llm:`` block from config.yaml into an
    :class:`LLMConfig`.

    :param raw: The raw ``llm:`` mapping from config.yaml, or
        ``None`` if the block was absent. Example:
        ``{"model": "openai/gpt-4o", "temperature": 0.7}``.
    :param expand_env: Whether to expand ``${VAR}`` references in
        the connection block. ``False`` keeps literals as-is.
    :returns: A populated :class:`LLMConfig`, or ``None`` when
        the ``llm:`` block is absent.
    :raises OmnigentError: If the ``llm:`` block is present but
        missing the required ``model`` field.
    """
    if raw is None:
        return None
    model = raw.get("model")
    if model is None:
        raise OmnigentError(
            "llm block present but missing required field: model",
            code=ErrorCode.INVALID_INPUT,
        )
    # ``connection``, ``profile``, ``request_timeout``, and ``retry``
    # are separated into their own typed fields; everything else is
    # passed through to the LLM SDK as extra kwargs.
    connection_raw = raw.get("connection")
    connection: dict[str, str] | None = None
    if isinstance(connection_raw, dict):
        raw_dict = {str(k): str(v) for k, v in connection_raw.items()}
        # Expand ${VAR} references so api_key: ${OPENAI_API_KEY} works.
        # Skipped when expand_env is False (scaffolding/validation).
        connection = expand_env_vars(raw_dict) if expand_env else raw_dict
    profile_raw = raw.get("profile")
    profile = str(profile_raw) if profile_raw is not None else None
    request_timeout = int(raw["request_timeout"]) if "request_timeout" in raw else 300
    retry = _parse_retry(raw.get("retry"))
    reserved = {"model", "connection", "profile", "request_timeout", "retry"}
    extra = {k: v for k, v in raw.items() if k not in reserved}
    return LLMConfig(
        model=str(model),
        extra=extra,
        connection=connection,
        profile=profile,
        request_timeout=request_timeout,
        retry=retry,
    )

def _parse_interaction(
    raw: dict[str, Any] | None,
) -> InteractionConfig:
    """
    Parse the ``interaction:`` block from config.yaml into an
    :class:`InteractionConfig`.

    :param raw: The raw ``interaction:`` mapping from config.yaml,
        or ``None`` if the block was absent. Example:
        ``{"conversational": false, "modalities": {"input":
        ["text", "image"]}}``.
    :returns: A populated :class:`InteractionConfig`. Returns
        defaults when *raw* is ``None``.
    """
    if raw is None:
        return InteractionConfig()
    modalities_raw = raw.get("modalities")
    if not isinstance(modalities_raw, dict):
        modalities = ModalityConfig()
    else:
        modalities = ModalityConfig(
            input=modalities_raw.get("input", ["text"]),
            output=modalities_raw.get("output", ["text"]),
        )
    conversational = raw.get("conversational", True)
    return InteractionConfig(
        conversational=bool(conversational),
        modalities=modalities,
    )

def _parse_compaction(
    raw: dict[str, Any] | None,
) -> CompactionConfig | None:
    """
    Parse the ``compaction:`` block from config.yaml into a
    :class:`CompactionConfig`.

    :param raw: The raw ``compaction:`` mapping from config.yaml, or
        ``None`` if the block was absent. Example:
        ``{"trigger_threshold": 0.8, "recent_window": 5}``.
    :returns: A populated :class:`CompactionConfig`, or ``None`` when
        the ``compaction:`` block is absent.
    """
    if raw is None:
        return None
    return CompactionConfig(
        trigger_threshold=float(raw.get("trigger_threshold", 0.8)),
        recent_window=int(raw.get("recent_window", 5)),
    )

def parse_server_llm(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> LLMConfig | None:
    """
    Parse the ``llm:`` block from the server ``--config`` YAML.

    Delegates to :func:`_parse_llm` — same grammar as the agent-level
    ``llm:`` block. Exposed as a public entry point so the CLI can
    call it without reaching into parser internals.

    :param raw: The ``llm:`` value from the server config YAML,
        e.g. ``{"model": "openai/gpt-4o-mini", "connection": {"api_key": "..."}}``.
        ``None`` when the key is absent.
    :param expand_env: Whether to expand ``${VAR}`` references in
        the connection block. ``True`` for production; ``False``
        for validation contexts where env vars may not be set.
    :returns: A :class:`LLMConfig` or ``None``.
    """
    return _parse_llm(raw, expand_env=expand_env)


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _capabilities as _sib_capabilities
    from . import _core as _sib_core
    from . import _credentials as _sib_credentials
    from . import _discover as _sib_discover
    from . import _guardrails as _sib_guardrails
    from . import _helpers as _sib_helpers
    from . import _mcp as _sib_mcp
    from . import _os_env as _sib_os_env
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
