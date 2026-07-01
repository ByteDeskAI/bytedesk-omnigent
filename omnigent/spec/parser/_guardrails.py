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

def _parse_guardrails(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> GuardrailsSpec | None:
    """
    Parse the ``guardrails:`` block into a :class:`GuardrailsSpec`.

    Returns ``None`` when the block is absent entirely — the
    runtime builds a no-op policy engine in that case
    (POLICIES.md §10 zero-policy case).

    :param raw: The ``guardrails:`` mapping from config.yaml,
        or ``None`` when the block was absent. Example:
        ``{"labels": {"integrity": {"initial": "1",
        "values": ["0", "1"], "monotonic": "decreasing"}},
        "policies": {"block_canada_input": {"type": "prompt",
        ...}}, "ask_timeout": 30}``.
    :param expand_env: Whether to expand ``${VAR}`` references
        in any nested ``llm.connection`` blocks (PromptPolicy
        LLM overrides). Propagated to :func:`_parse_llm`.
    :returns: A populated :class:`GuardrailsSpec`, or ``None``
        when *raw* is ``None``.
    :raises OmnigentError: On any spec-load validation
        failure (unknown phases, empty ``on:`` lists, invalid
        label defs, bad policy types, etc.).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"guardrails: must be a mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return GuardrailsSpec(
        labels=_parse_label_defs(raw.get("labels")),
        policies=_parse_policies(raw.get("policies"), expand_env=expand_env),
        ask_timeout=_parse_guardrails_ask_timeout(
            raw.get("ask_timeout", DEFAULT_ASK_TIMEOUT),
        ),
    )

def _parse_guardrails_ask_timeout(raw: Any) -> int:
    """
    Validate and coerce the spec-wide ``ask_timeout`` value.

    Accepts an integer (or string that parses as one);
    rejects ``<= 0`` at spec load per POLICIES.md §13. The
    ambiguity between "instant DENY" and "wait forever"
    drove the strict > 0 rule — both intents have explicit
    paths (omit ASK from action list; use a large finite
    number).

    :param raw: Raw ``guardrails.ask_timeout:`` value.
    :returns: Validated timeout in seconds.
    :raises OmnigentError: On non-integer or non-positive
        values.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise OmnigentError(
            f"guardrails.ask_timeout must be an integer, got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if value <= 0:
        raise OmnigentError(
            "guardrails.ask_timeout must be > 0 "
            "(omit ASK from policy action list for instant-DENY; "
            "use large finite values for long waits)",
            code=ErrorCode.INVALID_INPUT,
        )
    return value

def _parse_label_defs(
    raw: dict[str, Any] | None,
) -> dict[str, LabelDef] | None:
    """
    Parse the ``guardrails.labels:`` block into a dict of
    :class:`LabelDef` by key.

    Accepts three YAML shapes per POLICIES.md §3.1:

    - Bare string: ``integrity: "1"`` → schemaless with
      ``initial="1"``.
    - Dict (schema'd with initial):
      ``{initial: "1", values: [...], monotonic: ...}``.
    - Dict (schema'd without initial):
      ``{values: [...], monotonic: ...}``.

    :param raw: The ``labels:`` mapping, or ``None``.
    :returns: Dict mapping each label key to its
        :class:`LabelDef`. ``None`` when *raw* is ``None``.
    :raises OmnigentError: On malformed entries — empty
        dict, ``initial`` not in ``values``, unknown
        ``monotonic`` direction, etc.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"guardrails.labels: must be a mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    defs: dict[str, LabelDef] = {}
    for key, entry in raw.items():
        defs[str(key)] = _parse_single_label_def(str(key), entry)
    return defs

def _coerce_label_initial(raw: Any) -> str | None:
    """Coerce an ``initial:`` value to ``str | None``."""
    return None if raw is None else str(raw)

def _coerce_label_values(key: str, raw: Any) -> list[str] | None:
    """
    Coerce a ``values:`` list to ``list[str]`` or ``None``.

    :param key: Label key, for error messages.
    :param raw: Raw ``values:`` value from YAML.
    :returns: Every element str-coerced; ``None`` when
        *raw* is ``None``.
    :raises OmnigentError: When *raw* is a non-list
        non-None value.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"label {key!r}: `values` must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return [str(v) for v in raw]

def _coerce_label_monotonic(
    key: str,
    raw: Any,
) -> Literal["increasing", "decreasing"] | None:
    """
    Validate a ``monotonic:`` direction.

    :param key: Label key, for error messages.
    :param raw: Raw ``monotonic:`` value from YAML — must
        be ``"increasing"``, ``"decreasing"``, or absent.
    :returns: The validated direction, or ``None`` when
        *raw* is ``None``.
    :raises OmnigentError: On any other value.
    """
    if raw is None:
        return None
    if raw == "increasing":
        return "increasing"
    if raw == "decreasing":
        return "decreasing"
    raise OmnigentError(
        f"label {key!r}: `monotonic` must be 'increasing' or 'decreasing', got {raw!r}",
        code=ErrorCode.INVALID_INPUT,
    )

def _validate_label_def_cross_fields(
    key: str,
    initial: str | None,
    values: list[str] | None,
    monotonic: Literal["increasing", "decreasing"] | None,
) -> None:
    """
    Enforce cross-field constraints on a :class:`LabelDef`.

    Per POLICIES.md §13:

    - ``monotonic`` requires ``values`` (no positions to
      order without them).
    - When both ``initial`` and ``values`` are declared,
      ``initial`` must be in ``values``.

    :param key: Label key, for error messages.
    :param initial: Pre-coerced initial value.
    :param values: Pre-coerced values list.
    :param monotonic: Pre-validated direction.
    :raises OmnigentError: On any cross-field violation.
    """
    if monotonic is not None and values is None:
        raise OmnigentError(
            f"label {key!r}: `monotonic` requires a `values` list to order against",
            code=ErrorCode.INVALID_INPUT,
        )
    if initial is not None and values is not None and initial not in values:
        raise OmnigentError(
            f"label {key!r}: `initial` value {initial!r} is not in declared `values` {values!r}",
            code=ErrorCode.INVALID_INPUT,
        )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _capabilities as _sib_capabilities
    from . import _core as _sib_core
    from . import _credentials as _sib_credentials
    from . import _discover as _sib_discover
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
    for _key, _value in _sib_core.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_credentials.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_discover.__dict__.items():
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
