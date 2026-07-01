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

def _parse_policies(
    raw: dict[str, Any] | list[Any] | None,
    *,
    expand_env: bool = True,
) -> list[PolicySpec] | None:
    """
    Parse the ``guardrails.policies:`` block.

    YAML uses a mapping keyed by policy name (preserving
    YAML declaration order, which the engine relies on per
    POLICIES.md §4). Returns a list of
    :class:`PolicySpec` instances in that order.

    :param raw: The ``policies:`` mapping, or ``None``.
    :param expand_env: Propagated to
        :func:`_parse_llm` for any PromptPolicy ``llm:``
        overrides.
    :returns: List of policy specs, or ``None`` when *raw*
        is ``None``.
    :raises OmnigentError: On any malformed policy entry.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"guardrails.policies: must be a mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    policies: list[PolicySpec] = []
    for name, entry in raw.items():
        policies.append(
            _parse_policy_spec(str(name), entry, expand_env=expand_env),
        )
    return policies

def _parse_policy_spec(
    name: str,
    data: Any,
    *,
    expand_env: bool = True,
) -> PolicySpec:
    """
    Parse one policy's YAML block into the appropriate
    :class:`PolicySpec` subclass.

    Dispatches on the ``type:`` discriminator
    (``"function"``, ``"prompt"``, or ``"label"``).

    :param name: YAML key for this policy, used in error
        messages and recorded on the spec.
    :param data: Raw mapping from YAML (the value beneath
        ``policies.<name>:``).
    :param expand_env: Propagated for any nested ``llm:``
        connection overrides.
    :returns: A concrete ``PolicySpec`` subclass instance.
    :raises OmnigentError: On malformed data or unknown
        policy type.
    """
    del expand_env  # Was used by _parse_prompt_policy (removed).
    if not isinstance(data, dict):
        raise OmnigentError(
            f"policy {name!r}: must be a mapping, got {type(data).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    policy_type = data.get("type")
    if policy_type is None:
        raise OmnigentError(
            f"policy {name!r}: missing required field `type` (must be 'function')",
            code=ErrorCode.INVALID_INPUT,
        )
    if policy_type == "prompt":
        raise OmnigentError(
            f"policy {name!r}: type 'prompt' is no longer supported. "
            f"Use type 'function' with handler "
            f"'omnigent.policies.builtins.prompt.prompt_policy' instead.",
            code=ErrorCode.INVALID_INPUT,
        )
    base_kwargs = _parse_policy_base_fields(name, data, is_function=policy_type == "function")
    if policy_type == "function":
        return _parse_function_policy(name, data, base_kwargs)
    raise OmnigentError(
        f"policy {name!r}: unknown type {policy_type!r} (must be 'function')",
        code=ErrorCode.INVALID_INPUT,
    )

def _parse_policy_base_fields(
    name: str,
    data: dict[str, Any],
    *,
    is_function: bool = False,
) -> dict[str, Any]:
    """
    Parse the fields every policy type shares.

    Factored out of ``_parse_policy_spec`` so the dispatch
    function stays small. Fields: ``name``, ``on`` (with
    the ``[request, response]`` default per POLICIES.md §3.1),
    ``condition``, and per-policy ``ask_timeout`` override.

    For ``type: function`` policies (``is_function=True``) the
    ``on`` field is ignored — the callable self-selects which
    events to handle by returning ALLOW for events it doesn't act on.

    :param name: Enclosing policy name.
    :param data: Raw YAML mapping for this policy.
    :param is_function: ``True`` when parsing a ``type: function``
        policy. Ignores the ``on:`` field and sets ``on=None``.
    :returns: Kwargs dict ready to splat into any
        :class:`PolicySpec` subclass constructor.
    """
    if is_function:
        # ``on:`` is ignored for function policies — the callable self-selects
        # which events to handle by returning ALLOW for events it doesn't act on.
        on_value = None
    else:
        on_value = _parse_on(data.get("on", ["request", "response"]), policy_name=name)
    return {
        "name": name,
        "on": on_value,
        "condition": _parse_condition(data.get("condition"), policy_name=name),
        "ask_timeout": _parse_policy_ask_timeout(
            data.get("ask_timeout"),
            policy_name=name,
        ),
    }

def _parse_function_policy(
    name: str,
    data: dict[str, Any],
    base_kwargs: dict[str, Any],
) -> FunctionPolicySpec:
    """
    Parse a ``type: function`` policy block.

    :param name: Enclosing policy name (error messages +
        recorded on the spec).
    :param data: Raw YAML mapping for this policy.
    :param base_kwargs: Pre-parsed fields shared across
        policy types (``name``, ``on``, ``condition``,
        ``ask_timeout``).
    :returns: A populated :class:`FunctionPolicySpec`.
    :raises OmnigentError: On missing ``function:`` field
        or malformed ``action`` / ``set_labels`` values.
    """
    # Accept both ``function:`` and ``handler:`` for the callable path.
    # ``handler`` is the proto/service-policies convention; ``function``
    # is the original omnigent YAML convention.
    function_raw = data.get("function") or data.get("handler")
    if function_raw is None:
        raise OmnigentError(
            f"policy {name!r}: `function` policies require a `function:` or `handler:` field",
            code=ErrorCode.INVALID_INPUT,
        )
    action = _parse_action_list(data["action"], policy_name=name) if "action" in data else None
    set_labels = (
        _parse_writable_labels(data["set_labels"], policy_name=name)
        if "set_labels" in data
        else None
    )
    config = data.get("config")
    if config is not None and not isinstance(config, dict):
        raise OmnigentError(
            f"policy {name!r}: 'config' must be a dict, got {type(config).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return FunctionPolicySpec(
        **base_kwargs,
        function=_parse_function_ref(function_raw, policy_name=name),
        action=action,
        set_labels=set_labels,
        config=config,
    )

def _parse_on(
    raw: Any,
    *,
    policy_name: str,
) -> list[PhaseSelector]:
    """
    Parse a policy's ``on:`` list into :class:`PhaseSelector`
    entries.

    YAML shapes:
    - ``"request"`` → wildcard selector for the REQUEST phase.
    - ``"tool_call:web_search"`` → TOOL_CALL narrowed to
      one tool name.

    Tool-name narrowing is rejected on REQUEST / RESPONSE phases
    (only meaningful for tool_call / tool_result).

    :param raw: The ``on:`` value from YAML. Must be a
        non-empty list of strings.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: List of :class:`PhaseSelector` entries, one
        per YAML list element.
    :raises OmnigentError: On empty list, unknown phase,
        or tool-narrowing on a non-tool phase.
    """
    if not isinstance(raw, list):
        raise OmnigentError(
            f"policy {policy_name!r}: `on:` must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not raw:
        # POLICIES.md §13: empty `on:` creates a policy that
        # never fires — reject at spec load.
        raise OmnigentError(
            f"policy {policy_name!r}: `on:` must contain at least one "
            f"phase selector (empty list would create a policy that "
            f"never fires)",
            code=ErrorCode.INVALID_INPUT,
        )
    return [_parse_on_entry(entry, policy_name=policy_name) for entry in raw]

def _parse_on_entry(
    entry: Any,
    *,
    policy_name: str,
) -> PhaseSelector:
    """
    Parse one entry of a policy's ``on:`` list.

    Handles both forms: bare ``"<phase>"`` (wildcard) and
    ``"<phase>:<tool_name>"`` (tool-narrowed). Tool narrowing
    is rejected on phases other than TOOL_CALL / TOOL_RESULT.

    :param entry: One YAML list element — must be a string.
    :param policy_name: Enclosing policy name, used in error
        messages.
    :returns: A populated :class:`PhaseSelector`.
    :raises OmnigentError: On non-string entry, empty
        tool-name suffix, unknown phase, or tool narrowing
        on a non-tool phase.
    """
    if not isinstance(entry, str):
        raise OmnigentError(
            f"policy {policy_name!r}: `on:` entries must be strings, got {type(entry).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if ":" not in entry:
        return PhaseSelector(phase=_resolve_phase(entry, entry, policy_name=policy_name))
    phase_str, tool_name = entry.split(":", 1)
    if not tool_name:
        raise OmnigentError(
            f"policy {policy_name!r}: empty tool name in on-selector {entry!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    phase = _resolve_phase(phase_str, entry, policy_name=policy_name)
    if phase not in (Phase.TOOL_CALL, Phase.TOOL_RESULT):
        raise OmnigentError(
            f"policy {policy_name!r}: phase {phase.value!r} "
            f"cannot be narrowed by tool name; tool filters "
            f"only apply to tool_call / tool_result",
            code=ErrorCode.INVALID_INPUT,
        )
    return PhaseSelector(phase=phase, tool_name=tool_name)

def _parse_condition(
    raw: Any,
    *,
    policy_name: str,
) -> dict[str, str | list[str]] | None:
    """
    Parse a policy's ``condition:`` label-gate.

    Values are coerced to strings — label storage is always
    string-valued, and a YAML author writing
    ``condition: {integrity: 0}`` (unquoted int) would
    otherwise produce a silent runtime mismatch against the
    stored ``"0"``. The coercion matches omnigent parity
    for label values.

    :param raw: The ``condition:`` value from YAML, or
        ``None`` / absent.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: Dict mapping key → string value or list of
        string values. ``None`` when *raw* is absent OR when
        *raw* is an empty dict — both mean "always match."
    :raises OmnigentError: On a non-dict value.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"policy {policy_name!r}: `condition:` must be a mapping, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not raw:
        # Empty condition matches everything — equivalent to
        # omitting the field. Treated identically by returning
        # ``None`` here so downstream label-gate evaluation
        # takes the always-match short-circuit. (Earlier
        # revisions rejected ``{}`` as a typo guard; the guard
        # produced false positives on policies whose author
        # intended "match any labels, filter only by ``on:``".)
        return None
    coerced: dict[str, str | list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            coerced[str(key)] = [str(v) for v in value]
        else:
            coerced[str(key)] = str(value)
    return coerced

def _parse_action_list(
    raw: Any,
    *,
    policy_name: str,
) -> list[PolicyAction]:
    """
    Parse a policy's ``action:`` whitelist into a list of
    :class:`PolicyAction` enums.

    Accepts a bare string (single-element list sugar) or a
    list of strings. Validates each entry against the enum.

    :param raw: The ``action:`` value from YAML.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: List of :class:`PolicyAction` values.
    :raises OmnigentError: On empty list or unknown
        action value.
    """
    if isinstance(raw, str):
        strings = [raw]
    elif isinstance(raw, list):
        strings = [str(s) for s in raw]
    else:
        raise OmnigentError(
            f"policy {policy_name!r}: `action:` must be a string or "
            f"list of strings, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not strings:
        raise OmnigentError(
            f"policy {policy_name!r}: `action:` list must be non-empty",
            code=ErrorCode.INVALID_INPUT,
        )
    actions: list[PolicyAction] = []
    for s in strings:
        try:
            actions.append(PolicyAction(s))
        except ValueError as exc:
            raise OmnigentError(
                f"policy {policy_name!r}: invalid action {s!r} "
                f"(must be one of 'allow', 'ask', 'deny')",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
    return actions

def _parse_writable_labels(
    raw: Any,
    *,
    policy_name: str,
) -> list[str] | None:
    """
    Parse a policy's ``set_labels:`` whitelist (list form —
    used on PromptPolicy and FunctionPolicy).

    :param raw: The ``set_labels:`` list of allowed label
        keys (or ``None`` / absent).
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: List of allowed label keys, or ``None`` when
        *raw* is absent.
    :raises OmnigentError: When *raw* is not a list.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"policy {policy_name!r}: `set_labels:` must be a list "
            f"of label keys, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return [str(k) for k in raw]

def _parse_function_ref(
    raw: Any,
    *,
    policy_name: str,
) -> FunctionRef:
    """
    Parse a ``function:`` YAML value into a :class:`FunctionRef`.

    Two accepted shapes:

    - Bare string: dotted import path of the evaluator
      callable.
    - Dict: ``{path: ..., arguments: {...}}`` — path resolves
      to a factory called with ``arguments`` kwargs at
      workflow start.

    :param raw: The raw ``function:`` value from YAML.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: A populated :class:`FunctionRef`.
    :raises OmnigentError: On malformed shape — non-string
        path, missing path in dict form, non-dict
        ``arguments``.
    """
    if isinstance(raw, str):
        if not raw:
            raise OmnigentError(
                f"policy {policy_name!r}: `function:` path must be non-empty",
                code=ErrorCode.INVALID_INPUT,
            )
        return FunctionRef(path=raw, arguments=None)
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"policy {policy_name!r}: `function:` must be a dotted-path "
            f"string or a dict with {{path, arguments}}, got "
            f"{type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise OmnigentError(
            f"policy {policy_name!r}: `function.path` must be a non-empty dotted-path string",
            code=ErrorCode.INVALID_INPUT,
        )
    args = raw.get("arguments")
    if args is not None and not isinstance(args, dict):
        raise OmnigentError(
            f"policy {policy_name!r}: `function.arguments` must be a "
            f"mapping (or omitted), got {type(args).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return FunctionRef(path=path, arguments=args)

def _parse_policy_ask_timeout(
    raw: Any,
    *,
    policy_name: str,
) -> int | None:
    """
    Parse a per-policy ``ask_timeout:`` override.

    ``None`` / absent = fall back to the guardrails-level
    default. Values ``<= 0`` are rejected (POLICIES.md §13).

    :param raw: The ``ask_timeout:`` value from YAML.
    :param policy_name: Enclosing policy name for error
        messages.
    :returns: Integer override in seconds, or ``None`` when
        *raw* is absent.
    :raises OmnigentError: On non-integer or non-positive
        value.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise OmnigentError(
            f"policy {policy_name!r}: `ask_timeout` must be an integer, got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if value <= 0:
        raise OmnigentError(
            f"policy {policy_name!r}: `ask_timeout` must be > 0 "
            f"(omit ASK from the policy's action list for instant-DENY)",
            code=ErrorCode.INVALID_INPUT,
        )
    return value

def parse_default_policies(
    raw: dict[str, Any] | None,
    *,
    expand_env: bool = True,
) -> list[PolicySpec]:
    """
    Parse the ``policies:`` mapping from the server ``--config``
    YAML into a list of :class:`PolicySpec` instances.

    The YAML shape is a mapping keyed by policy name — the same grammar
    as ``guardrails.policies:`` in an agent spec:

    .. code-block:: yaml

        policies:
          admin__audit_tool_calls:
            type: function
            function: myorg.policies.audit
          admin__deny_pii_output:
            type: prompt
            on: [response]
            action: [allow, deny]
            prompt: "Deny if the response contains PII..."

    For ``type: function`` policies the ``on:`` field is ignored —
    the callable self-selects which phases to act on.

    Returns an empty list when *raw* is ``None`` or an empty mapping —
    the server starts up with no default policies in that case.

    :param raw: The ``policies:`` value from the server config
        YAML, e.g. ``{"admin__audit": {"type": "function",
        "function": "myorg.policies.audit"}}``. ``None`` when the key
        is absent.
    :param expand_env: Whether to expand ``${VAR}`` references in any
        nested ``llm.connection`` blocks (PromptPolicy LLM overrides).
        ``True`` for production; ``False`` for validation contexts
        where env vars may not be set.
    :returns: Ordered list of :class:`PolicySpec` instances ready for
        the policy engine. Empty list when *raw* is ``None`` or ``{}``.
    :raises OmnigentError: On any malformed policy entry — unknown
        type, missing required field, invalid phase selector, etc.
    """
    if not raw:
        return []
    return _parse_policies(raw, expand_env=expand_env) or []


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
    for _key, _value in _sib_os_env.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_skills.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_tools.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
