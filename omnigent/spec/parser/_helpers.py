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

def _validate_supervisor_tool_entry(  # type: ignore[explicit-any]
    index: int,
    entry: object,
    *,
    expand_env: bool = True,
) -> dict[str, Any]:
    """
    Validate one entry in the supervisor ``tools:`` list and return
    its verbatim dict form (with ``${VAR}`` references resolved).

    The real Databricks Supervisor API rejects flat tool entries; it
    expects each entry to NEST its config under a key matching the
    declared ``type``::

        {"type": "uc_connection",
         "uc_connection": {"name": "...", "description": "..."}}

    Validation order:

    1. Outer entry is a mapping with a ``type`` key in the supported
       set (:func:`_extract_supervisor_tool_type`).
    2. Nested ``<type>:`` sub-dict is present
       (:func:`_extract_supervisor_tool_nested`).
    3. (When *expand_env* is True) ``${VAR}`` / ``$VAR`` references in
       string values inside the sub-dict are expanded via
       :func:`expand_env_vars` and unresolved refs fail loud.
    4. Required fields per type are present
       (:func:`_check_supervisor_tool_required_fields`) — checked
       AFTER expansion so a YAML like ``id: ${SET_TO_EMPTY}``
       triggers the missing-field error rather than silently
       passing an empty string to the gateway.

    :param index: Position of *entry* in the original ``tools:`` list.
    :param entry: One element from the parsed YAML list.
    :param expand_env: Whether to expand ``${VAR}`` references in
        nested string values. Default ``True``; ``False`` for
        scaffolding paths.
    :returns: The validated nested entry, with strings expanded.
    :raises OmnigentError: On any validation failure.
    """
    if not isinstance(entry, dict):
        raise OmnigentError(
            f"tools[{index}] must be a YAML mapping when the "
            f"supervisor harness is selected, got "
            f"{type(entry).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    type_str = _extract_supervisor_tool_type(index, entry)
    nested = _extract_supervisor_tool_nested(index, entry, type_str)
    if expand_env:
        nested = _expand_supervisor_tool_env_vars(index, type_str, nested)
    _check_supervisor_tool_required_fields(index, type_str, nested)
    # Round-trip the nested entry verbatim. Stringify only the
    # outer ``type`` key (it's always a string and we want the
    # round-trip to be predictable); the inner sub-dict is already
    # a plain dict thanks to env-expansion above.
    return {
        "type": type_str,
        type_str: dict(nested),
    }

def _expand_supervisor_tool_env_vars(  # type: ignore[explicit-any]
    index: int,
    type_str: str,
    nested: dict[str, Any],
) -> dict[str, Any]:
    """
    Run ``${VAR}`` expansion over the string-valued fields of a
    supervisor tool's nested config.

    Non-string values (booleans, numbers, nested dicts) pass
    through unchanged — the expansion convention applies only to
    strings, which is where the Supervisor API places connector
    names, IDs, and descriptions.

    :param index: Position in the original ``tools:`` list (for
        error messages).
    :param type_str: The validated tool type, e.g. ``"genie_space"``.
    :param nested: The nested config sub-dict.
    :returns: A new dict with string values expanded.
    :raises OmnigentError: If a string value contains an
        unresolved ``${VAR}`` reference.
    """
    string_values = {k: v for k, v in nested.items() if isinstance(v, str)}
    other_values = {k: v for k, v in nested.items() if not isinstance(v, str)}
    try:
        expanded = expand_env_vars(string_values)
    except OmnigentError as exc:
        # Decorate the existing message with the offending tool
        # entry's index/type so the user knows which YAML field
        # to fix without having to grep for the unresolved var.
        raise OmnigentError(
            f"tools[{index}] (type={type_str!r}): {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return {**other_values, **expanded}

def _extract_supervisor_tool_type(  # type: ignore[explicit-any]
    index: int,
    entry: dict[str, Any],
) -> str:
    """
    Pull and validate the outer ``type`` key on a supervisor tool entry.

    :param index: Position in the original ``tools:`` list (for
        error messages).
    :param entry: The entry dict, already known to be a mapping.
    :returns: The validated type string, guaranteed to be a key
        in :data:`_SUPERVISOR_TOOL_REQUIRED_FIELDS`.
    :raises OmnigentError: When ``type`` is missing or unsupported.
    """
    supported = sorted(_SUPERVISOR_TOOL_REQUIRED_FIELDS)
    type_value = entry.get("type")
    if type_value is None:
        raise OmnigentError(
            f"tools[{index}] is missing required key 'type' (must be one of {supported})",
            code=ErrorCode.INVALID_INPUT,
        )
    type_str = str(type_value)
    if type_str not in _SUPERVISOR_TOOL_REQUIRED_FIELDS:
        raise OmnigentError(
            f"tools[{index}].type {type_str!r} is not a "
            f"supported supervisor tool type — must be one "
            f"of {supported}",
            code=ErrorCode.INVALID_INPUT,
        )
    return type_str

def _extract_supervisor_tool_nested(  # type: ignore[explicit-any]
    index: int,
    entry: dict[str, Any],
    type_str: str,
) -> dict[str, Any]:
    """
    Pull the nested ``<type>:`` sub-dict from a supervisor tool entry.

    The Databricks Supervisor API requires the per-type config to
    live under a key matching the declared ``type``. Flat shapes
    (config keys at the entry's top level) are rejected.

    :param index: Position in the original ``tools:`` list.
    :param entry: The entry dict, already type-validated.
    :param type_str: The validated type string from
        :func:`_extract_supervisor_tool_type`.
    :returns: The nested config dict.
    :raises OmnigentError: When the nested mapping is missing or
        not a dict.
    """
    nested = entry.get(type_str)
    if not isinstance(nested, dict):
        raise OmnigentError(
            f"tools[{index}] (type={type_str!r}) must include a "
            f"nested {type_str!r} mapping with the tool's config "
            f"(the Databricks Supervisor API rejects flat shapes)",
            code=ErrorCode.INVALID_INPUT,
        )
    return nested

def _check_supervisor_tool_required_fields(  # type: ignore[explicit-any]
    index: int,
    type_str: str,
    nested: dict[str, Any],
) -> None:
    """
    Validate the per-type required fields on a supervisor tool's
    nested config.

    Required fields live INSIDE the nested sub-dict in the
    supervisor's API shape. Treat missing keys and empty/blank
    values the same — a YAML key like ``id:`` with no value
    collapses to None and should fail identically to a key omitted
    entirely.

    :param index: Position in the original ``tools:`` list.
    :param type_str: The validated type string.
    :param nested: The nested config dict.
    :raises OmnigentError: When any required field is missing or
        has a falsy value.
    """
    required = _SUPERVISOR_TOOL_REQUIRED_FIELDS[type_str]
    missing = sorted(field_name for field_name in required if not nested.get(field_name))
    if missing:
        raise OmnigentError(
            f"tools[{index}] (type={type_str!r}) is missing "
            f"required field(s) {missing} inside the nested "
            f"{type_str!r} block; required fields are "
            f"{sorted(required)}",
            code=ErrorCode.INVALID_INPUT,
        )

def _parse_single_label_def(key: str, entry: Any) -> LabelDef:
    """
    Parse one label definition entry.

    :param key: The label key, used in error messages, e.g.
        ``"integrity"``.
    :param entry: Either a string (shorthand: value becomes
        ``initial``) or a dict with one or more of
        ``initial``, ``values``, ``monotonic``.
    :returns: A populated :class:`LabelDef`.
    :raises OmnigentError: On any malformed value.
    """
    # Bare-string shorthand: `integrity: "1"` → initial only.
    if isinstance(entry, str):
        return LabelDef(initial=entry)
    if isinstance(entry, bool) or entry is None or isinstance(entry, int | float):
        # Coerce scalar to string for shorthand form. YAML
        # authors often write `: 1` expecting "1"; coercing
        # matches the condition-value coercion policy elsewhere.
        return LabelDef(initial=str(entry) if entry is not None else None)
    if not isinstance(entry, dict):
        raise OmnigentError(
            f"label {key!r} must be a string or mapping, got {type(entry).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not entry:
        # Empty-dict typo guard — matches POLICIES.md §13.
        raise OmnigentError(
            f"label {key!r} declares an empty dict — must contain at "
            f"least one of `initial`, `values`, or `monotonic`",
            code=ErrorCode.INVALID_INPUT,
        )
    initial = _coerce_label_initial(entry.get("initial"))
    values = _coerce_label_values(key, entry.get("values"))
    monotonic = _coerce_label_monotonic(key, entry.get("monotonic"))
    _validate_label_def_cross_fields(key, initial, values, monotonic)
    return LabelDef(initial=initial, values=values, monotonic=monotonic)

def _resolve_phase(
    phase_str: str,
    context: str,
    *,
    policy_name: str,
) -> Phase:
    """
    Resolve a phase-string into a :class:`Phase` enum.

    :param phase_str: The phase part of the selector
        (before any ``:``), e.g. ``"tool_call"``.
    :param context: Full on-selector value, used verbatim in
        the error message so the author can see which
        element failed, e.g. ``"tool_call:web_search"``.
    :param policy_name: Enclosing policy name, for error
        messages.
    :returns: The resolved :class:`Phase`.
    :raises OmnigentError: When *phase_str* is not a
        valid phase.
    """
    try:
        return Phase(phase_str)
    except ValueError as exc:
        raise OmnigentError(
            f"policy {policy_name!r}: unknown phase {phase_str!r} in {context!r}"
            if context != phase_str
            else f"policy {policy_name!r}: unknown phase {phase_str!r}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _capabilities as _sib_capabilities
    from . import _core as _sib_core
    from . import _credentials as _sib_credentials
    from . import _discover as _sib_discover
    from . import _guardrails as _sib_guardrails
    from . import _llm as _sib_llm
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
