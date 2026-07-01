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

def _parse_inline_mcp_servers(
    raw_tools: object,
    *,
    expand_env: bool = True,
) -> list[MCPServerConfig]:
    """
    Extract inline ``type: mcp`` entries from the top-level
    ``tools:`` block of config.yaml.

    The inline MCP format uses the YAML mapping key as the server
    name and derives the transport from the fields present:

    .. code-block:: yaml

        tools:
          github:
            type: mcp
            command: npx
            args: ["-y", "@modelcontextprotocol/server-github"]
          search:
            type: mcp
            url: https://mcp.example.com/sse
            headers:
              Authorization: "Bearer ${MCP_TOKEN}"

    ``type: mcp`` entries are those whose value is a dict containing
    ``type: mcp``. Standard :class:`ToolsConfig` keys (``agents``,
    ``builtins``, ``timeout``, ``retry``, ``sandbox``) are skipped
    even when they appear as dict values.

    Transport is inferred: ``command`` present → ``"stdio"``,
    ``url`` present → ``"http"``. Entries where neither is present
    (e.g. ``databricks_server``-only Databricks MCPs) are skipped —
    they don't have a local spawn or SSE endpoint to display.

    :param raw_tools: The raw value of the top-level ``tools:`` key
        in config.yaml. ``None`` or a non-dict value returns an empty
        list without raising.
    :param expand_env: Whether to expand ``${VAR}`` references in
        ``url``, ``headers``, and ``env`` values. ``True`` (default) for
        deploy/runtime; ``False`` for scaffolding/validation.
    :returns: A list of :class:`MCPServerConfig` objects, one per
        inline MCP entry, in YAML key order.
    """
    if not isinstance(raw_tools, dict):
        return []
    servers: list[MCPServerConfig] = []
    for key, val in raw_tools.items():
        if key in _TOOLS_CONFIG_KEYS:
            continue
        if not isinstance(val, dict):
            continue
        if str(val.get("type", "")) != "mcp":
            continue
        name = str(key)
        command = val.get("command")
        url = val.get("url")
        if command is not None:
            transport: str = "stdio"
        elif url is not None:
            transport = "http"
        else:
            # Databricks-managed server or unknown shape — no local
            # endpoint to display; skip.
            continue
        raw_args = val.get("args", [])
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        raw_headers = val.get("headers", {})
        if raw_headers and not isinstance(raw_headers, dict):
            raise OmnigentError(
                f"Inline MCP server {name!r} 'headers' must be a mapping",
                code=ErrorCode.INVALID_INPUT,
            )
        resolved_url = (
            expand_env_vars({"url": str(url)})["url"]
            if expand_env and url is not None
            else str(url)
            if url is not None
            else None
        )
        headers = expand_env_vars(raw_headers) if expand_env and raw_headers else raw_headers
        raw_env = val.get("env", {})
        if raw_env and not isinstance(raw_env, dict):
            raise OmnigentError(
                f"Inline MCP server {name!r} 'env' must be a mapping",
                code=ErrorCode.INVALID_INPUT,
            )
        env = expand_env_vars(raw_env) if expand_env and raw_env else raw_env
        # Optional Databricks auth — resolves a bearer token at
        # connection time from ~/.databrickscfg.
        raw_auth = val.get("auth")
        databricks_profile: str | None = None
        if isinstance(raw_auth, dict) and str(raw_auth.get("type", "")) == "databricks":
            raw_profile = raw_auth.get("profile")
            if raw_profile is None:
                raise OmnigentError(
                    f"Inline MCP server {name!r} auth type 'databricks' "
                    f"requires a 'profile' field",
                    code=ErrorCode.INVALID_INPUT,
                )
            databricks_profile = str(raw_profile)
        # Optional OAuth client-credentials auth — mints a bearer token at
        # connection time from token_url (provider-agnostic; e.g. an OpenIddict
        # resource server). Secret-bearing fields are env-expanded like headers.
        oauth: MCPOAuthConfig | None = None
        if isinstance(raw_auth, dict) and str(raw_auth.get("type", "")) == "oauth":
            scalars: dict[str, str] = {}
            for k in ("token_url", "client_id", "client_secret", "resource"):
                v = raw_auth.get(k)
                if v is not None:
                    scalars[k] = str(v)
            scalars = expand_env_vars(scalars) if expand_env and scalars else scalars
            for req in ("token_url", "client_id"):
                if not scalars.get(req):
                    raise OmnigentError(
                        f"Inline MCP server {name!r} auth type 'oauth' requires a '{req}' field",
                        code=ErrorCode.INVALID_INPUT,
                    )
            raw_scopes = raw_auth.get("scopes") or []
            scope_list = raw_scopes if isinstance(raw_scopes, list) else [raw_scopes]
            oauth = MCPOAuthConfig(
                token_url=scalars["token_url"],
                client_id=scalars["client_id"],
                client_secret=scalars.get("client_secret", ""),
                scopes=[str(s) for s in scope_list],
                resource=scalars.get("resource"),
            )
        servers.append(
            MCPServerConfig(
                name=name,
                transport=transport,
                # str() guards against non-string YAML scalars (int, bool, etc.)
                description=str(raw_desc)
                if (raw_desc := val.get("description")) is not None
                else None,
                url=resolved_url,
                command=str(command) if command is not None else None,
                args=args,
                headers=headers,
                env=env,
                databricks_profile=databricks_profile,
                oauth=oauth,
                tool_allowlist=_parse_tool_allowlist(val),
            )
        )
    return servers

def _parse_tool_allowlist(val: dict[str, Any]) -> list[str]:  # type: ignore[explicit-any]
    """
    Read an optional per-server tool allowlist from an inline MCP entry.

    Accepts ``tool_allowlist`` (canonical) or the ``tools``/``allow``
    aliases — ``tools`` matches the attribute the runner-side
    :class:`~omnigent.runner.mcp_manager.RunnerMcpManager` already reads.
    A missing or empty value yields ``[]`` (expose all tools).

    :param val: The inline ``tools.<name>`` mapping, e.g.
        ``{"type": "mcp", "url": "...", "tool_allowlist": ["a", "b"]}``.
    :returns: A list of bare tool names (stringified), or ``[]``.
    """
    for k in ("tool_allowlist", "tools", "allow"):
        raw = val.get(k)
        if raw:
            return [str(t) for t in raw] if isinstance(raw, list) else [str(raw)]
    return []

def _discover_mcp_servers(
    mcp_dir: Path,
    *,
    expand_env: bool = True,
) -> list[MCPServerConfig]:
    """
    Discover and parse all MCP server configs under
    ``tools/mcp/``.

    Each ``.yaml`` file in the directory is parsed into an
    :class:`MCPServerConfig`.

    :param mcp_dir: Path to the ``tools/mcp/`` directory, e.g.
        ``root / "tools" / "mcp"``.
    :param expand_env: Whether to expand ``${VAR}`` references in
        headers. ``False`` keeps literals as-is.
    :returns: A sorted list of parsed :class:`MCPServerConfig`
        objects. Returns an empty list if *mcp_dir* does not
        exist.
    :raises OmnigentError: If any YAML file is malformed or
        missing required fields (``name``, ``transport``).
    """
    if not mcp_dir.is_dir():
        return []
    servers: list[MCPServerConfig] = []
    for yaml_file in sorted(mcp_dir.glob("*.yaml")):
        raw = yaml.safe_load(yaml_file.read_text())
        if not isinstance(raw, dict):
            raise OmnigentError(
                f"MCP config must be a YAML mapping: {yaml_file}",
                code=ErrorCode.INVALID_INPUT,
            )
        name = raw.get("name")
        if name is None:
            raise OmnigentError(
                f"MCP config missing required field 'name': {yaml_file}",
                code=ErrorCode.INVALID_INPUT,
            )
        transport = raw.get("transport")
        if transport is None:
            raise OmnigentError(
                f"MCP config missing required field 'transport': {yaml_file}",
                code=ErrorCode.INVALID_INPUT,
            )
        transport_str = str(transport)
        if transport_str == "http":
            servers.append(_parse_http_mcp_server(name, raw, yaml_file, expand_env=expand_env))
        elif transport_str == "stdio":
            servers.append(_parse_stdio_mcp_server(name, raw, yaml_file, expand_env=expand_env))
        else:
            raise OmnigentError(
                f"MCP server {name!r} uses unsupported transport "
                f"{transport!r} — must be 'http' or 'stdio': {yaml_file}",
                code=ErrorCode.INVALID_INPUT,
            )
    return servers

def _parse_http_mcp_server(
    name: object,
    raw: dict[str, Any],  # type: ignore[explicit-any]
    yaml_file: Path,
    *,
    expand_env: bool,
) -> MCPServerConfig:
    """
    Parse an HTTP (SSE) MCP server YAML into an :class:`MCPServerConfig`.

    HTTP transport requires ``url``; ``url`` and ``headers`` are
    expanded via :func:`expand_env_vars` when *expand_env* is True.
    Stdio-only fields (``command``, ``args``, ``env``, ``sandbox``)
    are rejected loud — mixing transports silently would hide bugs
    in the YAML.

    :param name: The ``name`` field from the YAML (already validated
        non-None by the caller), e.g. ``"github"``.
    :param raw: Parsed YAML mapping for the MCP file, e.g.
        ``{"name": "github", "transport": "http", "url": "..."}``.
    :param yaml_file: Path to the source file — used in error messages.
    :param expand_env: Whether to expand ``${VAR}`` references in
        ``url`` and ``headers``.
    :returns: A fully populated :class:`MCPServerConfig` with
        ``transport == "http"``.
    :raises OmnigentError: If ``url`` is missing or a stdio-only
        field was supplied.
    """
    _reject_wrong_transport_keys(
        name,
        raw,
        yaml_file,
        disallowed=("command", "args", "env", "sandbox"),
        transport_name="http",
    )
    url = raw.get("url")
    if url is None:
        raise OmnigentError(
            f"MCP server {name!r} missing required field 'url': {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )
    resolved_url = expand_env_vars({"url": str(url)})["url"] if expand_env else str(url)
    return MCPServerConfig(
        name=str(name),
        transport="http",
        url=resolved_url,
        headers=(
            expand_env_vars(raw.get("headers", {})) if expand_env else raw.get("headers", {})
        ),
        description=raw.get("description"),
        timeout=int(raw["timeout"]) if "timeout" in raw else None,
        retry=_parse_retry(raw["retry"]) if "retry" in raw else None,
        tool_allowlist=_parse_tool_allowlist(raw),
    )

def _parse_stdio_mcp_server(
    name: object,
    raw: dict[str, Any],  # type: ignore[explicit-any]
    yaml_file: Path,
    *,
    expand_env: bool,
) -> MCPServerConfig:
    """
    Parse a stdio MCP server YAML into an :class:`MCPServerConfig`.

    Stdio transport requires ``command``; ``args`` and ``env`` are
    optional (default empty). ``sandbox`` defaults to ``True`` — the
    subprocess is srt-wrapped when possible. HTTP-only fields
    (``url``, ``headers``) are rejected loud.

    Environment values are expanded when *expand_env* is True so
    YAML like ``env: {GITHUB_TOKEN: \"${GITHUB_TOKEN}\"}`` resolves
    at parse time. ``args`` are NOT expanded — they're treated as
    a literal argv (consistent with how :class:`LocalToolInfo`
    treats command args).

    :param name: The ``name`` field from the YAML (already validated
        non-None by the caller).
    :param raw: Parsed YAML mapping, e.g.
        ``{"name": "github", "transport": "stdio", "command": "npx",
        "args": ["-y", "..."], "env": {"GITHUB_TOKEN": "${GH_TOKEN}"}}``.
    :param yaml_file: Path to the source file — used in error messages.
    :param expand_env: Whether to expand ``${VAR}`` references in
        ``env``.
    :returns: A fully populated :class:`MCPServerConfig` with
        ``transport == "stdio"``.
    :raises OmnigentError: If ``command`` is missing, ``args`` is
        not a list, ``env`` is not a mapping, or an HTTP-only field
        was supplied.
    """
    _reject_wrong_transport_keys(
        name,
        raw,
        yaml_file,
        disallowed=("url", "headers"),
        transport_name="stdio",
    )
    command = raw.get("command")
    if command is None:
        raise OmnigentError(
            f"MCP server {name!r} (transport='stdio') missing required field "
            f"'command': {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_args = raw.get("args", [])
    if not isinstance(raw_args, list):
        raise OmnigentError(
            f"MCP server {name!r} (transport='stdio') 'args' must be a list: {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_env = raw.get("env", {})
    if not isinstance(raw_env, dict):
        raise OmnigentError(
            f"MCP server {name!r} (transport='stdio') 'env' must be a mapping: {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )
    env = expand_env_vars(raw_env) if expand_env else raw_env
    if "sandbox" in raw:
        # Step 7: ``sandbox: <bool>`` was an AP-only no-op that
        # wrapped the stdio spawn with ``srt``. srt's default
        # policy blocks outbound network, which broke every
        # useful MCP server, so the field is gone. Reject loud
        # so authors who copy old YAMLs see the change instead
        # of a silently-ignored key. Future per-MCP sandboxing
        # will use a different schema (per-host outbound
        # allowlists) routed through the environments primitive.
        raise OmnigentError(
            f"MCP server {name!r} (transport='stdio') 'sandbox' field "
            f"was removed in step 7 of the harness contract migration: "
            f"{yaml_file}. The previous default (srt-wrap) blocked "
            f"outbound network and broke every useful MCP. Drop the "
            f"key from the YAML; future sandboxing will use a "
            f"per-MCP outbound-host allowlist with a different schema.",
            code=ErrorCode.INVALID_INPUT,
        )
    return MCPServerConfig(
        name=str(name),
        transport="stdio",
        command=str(command),
        args=[str(a) for a in raw_args],
        env={str(k): str(v) for k, v in env.items()},
        description=raw.get("description"),
        timeout=int(raw["timeout"]) if "timeout" in raw else None,
        retry=_parse_retry(raw["retry"]) if "retry" in raw else None,
        tool_allowlist=_parse_tool_allowlist(raw),
    )

def _reject_wrong_transport_keys(
    name: object,
    raw: dict[str, Any],  # type: ignore[explicit-any]
    yaml_file: Path,
    *,
    disallowed: tuple[str, ...],
    transport_name: str,
) -> None:
    """
    Fail loud if an MCP YAML mixes fields from the wrong transport.

    E.g. ``transport: http`` with a ``command:`` key, or
    ``transport: stdio`` with a ``url:`` key — both silently-ignored
    shapes would hide authoring bugs. Name every offending key in
    the error so the author can clean the YAML in one pass.

    :param name: The MCP server's ``name`` field, used in the error
        message.
    :param raw: Parsed YAML mapping.
    :param yaml_file: Path to the source file — used in error messages.
    :param disallowed: Tuple of keys that MUST NOT appear for this
        transport, e.g. ``("url", "headers")`` for stdio.
    :param transport_name: Human-readable transport label for the
        error message, e.g. ``"stdio"``.
    :raises OmnigentError: When any *disallowed* key is present
        in *raw*.
    """
    offenders = [k for k in disallowed if k in raw]
    if offenders:
        raise OmnigentError(
            f"MCP server {name!r} (transport={transport_name!r}) has "
            f"wrong-transport field(s) {offenders!r}: {yaml_file}",
            code=ErrorCode.INVALID_INPUT,
        )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _capabilities as _sib_capabilities
    from . import _core as _sib_core
    from . import _credentials as _sib_credentials
    from . import _discover as _sib_discover
    from . import _guardrails as _sib_guardrails
    from . import _helpers as _sib_helpers
    from . import _llm as _sib_llm
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
    for _key, _value in _sib_llm.__dict__.items():
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
