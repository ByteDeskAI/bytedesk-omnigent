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

class _CredentialSourceModel(BaseModel):
    """Pydantic boundary model for a ``credential_proxy[*].source`` mapping.

    The secret origin is a structured single-key mapping —
    ``{env: VAR}``, ``{file: path}``, or ``{command: cmd}`` — rather than
    a prefix-encoded string. Exactly one key must be set. Pydantic
    validates the shape here; :meth:`to_spec` converts it to the internal
    :class:`CredentialSourceSpec` dataclass the runtime consumes.

    :param env: Parent environment variable name carrying the secret,
        e.g. ``"OA_TEST_GITHUB_PAT"``.
    :param file: File path (``~`` expanded at resolution time) holding the
        secret, e.g. ``"~/.config/tokens/github_pat.txt"``.
    :param command: Shell command whose stdout is the secret, e.g.
        ``"gh auth token"``.
    """

    model_config = ConfigDict(extra="forbid")

    env: str | None = None
    file: str | None = None
    command: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> _CredentialSourceModel:
        """
        Require exactly one source key and validate its value.

        :returns: ``self`` once validated.
        :raises ValueError: If zero or multiple keys are set, ``env`` is
            not a POSIX environment variable name, or ``file`` / ``command``
            is blank.
        """
        set_keys = [
            name
            for name, value in (("env", self.env), ("file", self.file), ("command", self.command))
            if value is not None
        ]
        if len(set_keys) != 1:
            raise ValueError("source must set exactly one of 'env', 'file', or 'command'")
        if self.env is not None and not _ENV_VAR_NAME_RE.match(self.env):
            raise ValueError("source 'env' must be a POSIX environment variable name")
        if self.file is not None and not self.file.strip():
            raise ValueError("source 'file' must be a non-empty path")
        if self.command is not None and not self.command.strip():
            raise ValueError("source 'command' must be a non-empty command")
        return self

    def to_spec(self) -> CredentialSourceSpec:
        """
        Convert this validated model into a :class:`CredentialSourceSpec`.

        :returns: The internal dataclass the runtime resolves the secret
            from. Exactly one of ``env`` / ``file`` / ``command`` is set
            (guaranteed by :meth:`_exactly_one_source`).
        """
        if self.env is not None:
            return CredentialSourceSpec(kind="env", env=self.env)
        if self.file is not None:
            return CredentialSourceSpec(kind="file", path=self.file.strip())
        assert self.command is not None
        return CredentialSourceSpec(kind="command", command=self.command.strip())

class _CredentialProxyItemModel(BaseModel):
    """Pydantic boundary model for one raw ``credential_proxy`` entry.

    Validates the entry's *shape* — ``type``, ``source``, ``target`` /
    ``targets`` cardinality, the optional ``env`` injection shim, and the
    optional Basic ``username`` — replacing the hand-rolled per-field
    ``isinstance`` checks. The parser then normalizes each validated model
    into one or more :class:`CredentialProxyEntry` host bindings (the
    domain transformation pydantic can't express: host/path splitting,
    ``gh_basic`` git-vs-API split, default targets). Unknown keys are
    rejected (``extra="forbid"``) so typos fail loud.

    :param type: Credential preset / primitive, one of ``"https_bearer"``,
        ``"https_basic"``, ``"git_https"``, ``"gh_basic"``.
    :param source: Where the parent resolves the real secret from.
    :param target: A single ``host`` or ``host/path`` binding, e.g.
        ``"github.com/org/repo.git"``. Mutually exclusive with ``targets``.
    :param targets: A non-empty list of ``host`` / ``host/path`` bindings.
        Mutually exclusive with ``target``.
    :param env: Optional sandbox env var that receives the synthetic
        placeholder (opt-in injection shim for credential-gating clients);
        a POSIX environment variable name. Only accepted for the
        ``https_*`` primitives — ``git_https`` / ``gh_basic`` manage
        injection themselves.
    :param username: Optional Basic-auth username for ``https_basic`` /
        ``git_https`` (defaults to ``x-access-token``).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["https_bearer", "https_basic", "git_https", "gh_basic"]
    source: _CredentialSourceModel
    target: str | None = None
    targets: list[str] | None = None
    env: str | None = None
    username: str | None = None

    @field_validator("env")
    @classmethod
    def _env_is_posix(cls, value: str | None) -> str | None:
        """
        Reject an ``env`` that is not a POSIX environment variable name.

        :param value: The raw ``env`` value, or ``None`` when absent.
        :returns: ``value`` unchanged when valid.
        :raises ValueError: If ``env`` is present but malformed.
        """
        if value is not None and not _ENV_VAR_NAME_RE.match(value):
            raise ValueError("env must be a POSIX environment variable name")
        return value

    @field_validator("username")
    @classmethod
    def _username_nonempty(cls, value: str | None) -> str | None:
        """
        Reject an empty ``username``.

        :param value: The raw ``username`` value, or ``None`` when absent.
        :returns: ``value`` unchanged when valid.
        :raises ValueError: If ``username`` is present but empty.
        """
        if value is not None and not value:
            raise ValueError("username must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _check_target_cardinality(self) -> _CredentialProxyItemModel:
        """
        Enforce ``target`` / ``targets`` cardinality and per-type options.

        ``https_*`` and ``git_https`` require exactly one of ``target`` or
        ``targets``; ``gh_basic`` allows neither (it defaults to the
        GitHub git + API hosts) but not both. The ``env`` shim is only
        meaningful for the ``https_*`` primitives, and ``username`` only
        applies to the Basic schemes.

        :returns: ``self`` once validated.
        :raises ValueError: On a cardinality violation or a per-type
            option that does not apply.
        """
        has_target = self.target is not None
        has_targets = self.targets is not None
        if has_targets and not self.targets:
            raise ValueError("targets must be a non-empty list")
        if self.type == "gh_basic":
            if has_target and has_targets:
                raise ValueError("gh_basic accepts at most one of 'target' or 'targets'")
        elif has_target == has_targets:
            raise ValueError("must declare exactly one of 'target' or 'targets'")
        if self.env is not None and self.type in ("git_https", "gh_basic"):
            raise ValueError(f"{self.type} does not accept an 'env' injection shim")
        if self.username is not None and self.type == "https_bearer":
            raise ValueError("https_bearer does not accept a 'username'")
        return self

def _format_validation_error(exc: ValidationError) -> str:
    """
    Render a pydantic ``ValidationError`` as one compact line.

    The credential-proxy parser wraps pydantic failures in
    :class:`OmnigentError` so the CLI / loader surface a single error
    type. This flattens pydantic's structured errors into ``field:
    message`` clauses joined by ``; ``, keyed by the dotted field
    location.

    :param exc: The raised pydantic validation error.
    :returns: A semicolon-joined summary, e.g. ``"source: source must
        set exactly one of 'env', 'file', or 'command'"``. When the
        failure is on the entry root (e.g. a cardinality model
        validator), the location renders as ``"(entry)"``.
    """
    clauses: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "(entry)"
        clauses.append(f"{loc}: {err['msg']}")
    return "; ".join(clauses)

def _parse_credential_proxy(raw: object) -> CredentialProxySpec | None:
    """
    Parse and validate the ``credential_proxy:`` field of ``os_env.sandbox``.

    Each list entry declares one of four ``type`` values and is normalized
    into one or more :class:`CredentialProxyEntry` bindings. All four
    default to **swap-on-access** — nothing credential-shaped enters the
    sandbox; the egress proxy attaches the real credential to bound-host
    requests:

    - ``https_bearer``: ``target``/``targets`` + ``source`` + optional
      ``env``. Emits ``Authorization: Bearer <real>`` upstream.
    - ``https_basic``: ``target``/``targets`` + ``source`` + optional
      ``env`` + optional ``username``. Emits ``Authorization: Basic <real>``.
    - ``git_https``: ``target``/``targets`` + ``source`` + optional
      ``username``. Git over HTTPS via swap-on-access (Basic).
    - ``gh_basic``: ``source`` + optional ``targets``. Swap-on-access for
      the git host; injects ``GH_TOKEN`` / ``GITHUB_TOKEN`` for the API
      host because ``gh`` won't call without a local token.

    The optional ``env`` field is the opt-in injection shim for clients
    that refuse to issue a request without a local credential.

    Each entry's shape is validated by :class:`_CredentialProxyItemModel`
    (pydantic) before normalization; a :class:`pydantic.ValidationError`
    is re-raised as an :class:`OmnigentError` so callers see one error
    type.

    :param raw: Raw value from the YAML, e.g. ``[{"type": "git_https",
        "target": "github.com/org/repo.git", "source": {"env": "GH_PAT"}}]``,
        or ``None`` when the field is absent.
    :returns: A populated :class:`CredentialProxySpec`, or ``None`` when
        ``raw`` is absent or an empty list.
    :raises OmnigentError: If the value isn't a list, an entry fails
        validation (unknown ``type``, bad ``source``, target cardinality,
        etc.), or two entries bind the same host.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise OmnigentError(
            f"os_env.sandbox.credential_proxy must be a list, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not raw:
        return None
    entries: list[CredentialProxyEntry] = []
    for i, item in enumerate(raw):
        try:
            model = _CredentialProxyItemModel.model_validate(item)
        except ValidationError as exc:
            raise OmnigentError(
                f"os_env.sandbox.credential_proxy[{i}] is invalid: "
                f"{_format_validation_error(exc)}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        source = model.source.to_spec()
        if model.type == "gh_basic":
            entries.extend(_normalize_gh_basic(model, source=source, index=i))
        elif model.type == "https_bearer":
            entries.extend(_normalize_https_bearer(model, source=source, index=i))
        elif model.type == "https_basic":
            entries.extend(_normalize_https_basic(model, source=source, index=i))
        else:  # git_https
            entries.extend(_normalize_git_https(model, source=source, index=i))
    if not entries:
        return None
    # Fail loud on conflicting host bindings. The egress proxy keys its
    # rewrite table by host, so two entries binding the same host would
    # silently last-win (one credential dropped). Reject it at parse time
    # rather than picking a binding nondeterministically.
    seen_hosts: dict[str, str] = {}
    for entry in entries:
        host_key = entry.host.lower()
        if host_key in seen_hosts:
            raise OmnigentError(
                "os_env.sandbox.credential_proxy binds host "
                f"{entry.host!r} more than once (also via "
                f"{seen_hosts[host_key]!r}); each host may be bound by at "
                "most one credential. Remove the duplicate entry.",
                code=ErrorCode.INVALID_INPUT,
            )
        seen_hosts[host_key] = entry.host
    return CredentialProxySpec(entries=entries)

def _credential_proxy_macos_unsupported_reason(
    credential_proxy: CredentialProxySpec | None,
    sandbox_type: str,
) -> str | None:
    """
    Explain why a ``credential_proxy`` cannot work under ``darwin_seatbelt``.

    The ``gh_basic`` preset emits a ``token``-scheme binding for the GitHub
    API host (``api.*``) and injects ``GH_TOKEN`` / ``GITHUB_TOKEN`` so the
    GitHub CLI authenticates through the egress MITM proxy. ``gh`` is a Go
    binary, and Go on macOS verifies TLS against the system keychain via
    Security.framework -- it ignores ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``,
    which is exactly how the egress proxy publishes its MITM CA to sandboxed
    tools. ``gh`` therefore rejects the proxy's forged certificate and every
    ``gh`` call fails with ``"certificate is not trusted"``. Since
    ``darwin_seatbelt`` is macOS-only, the combination can never succeed, so we
    reject it at parse time instead of surfacing an opaque runtime TLS error.

    The ``token`` scheme is emitted only by ``gh_basic`` (see
    :func:`_normalize_gh_basic`); keying on the scheme rather than re-deriving
    the original ``type`` keeps this check independent of how the presets
    normalize into bindings.

    :param credential_proxy: Parsed credential-proxy spec, or ``None`` when the
        ``credential_proxy:`` field is absent.
    :param sandbox_type: Resolved sandbox backend, e.g. ``"darwin_seatbelt"``
        (macOS) or ``"linux_bwrap"`` (Linux).
    :returns: A human-readable rejection message when a ``gh_basic`` (i.e. a
        ``token``-scheme) binding is configured on ``darwin_seatbelt``, else
        ``None``.
    """
    if credential_proxy is None or sandbox_type != "darwin_seatbelt":
        return None
    if not any(entry.scheme == "token" for entry in credential_proxy.entries):
        return None
    return (
        "os_env.sandbox.credential_proxy type 'gh_basic' does not work on macOS "
        "(sandbox.type=darwin_seatbelt). 'gh_basic' wires the GitHub CLI 'gh', "
        "which is a Go binary, and Go on macOS verifies TLS against the system "
        "keychain (Security.framework) and ignores SSL_CERT_FILE -- the "
        "environment variable the egress MITM proxy uses to publish its CA to "
        "sandboxed tools. 'gh' therefore rejects the proxy's certificate and "
        "every 'gh' call fails with 'certificate is not trusted'. Use "
        "sandbox.type=linux_bwrap (Go honors SSL_CERT_FILE on Linux), or use "
        "the 'https_bearer' / 'https_basic' primitives with a non-Go client "
        "(curl / python / node) that trusts the proxy CA on macOS."
    )

def _normalize_https_bearer(
    model: _CredentialProxyItemModel,
    *,
    source: CredentialSourceSpec,
    index: int,
) -> list[CredentialProxyEntry]:
    """
    Normalize an ``https_bearer`` entry into per-host Bearer bindings.

    The default is swap-on-access: a tool makes its request with no
    ``Authorization`` header and the proxy injects ``Bearer <real>`` for
    the bound host. The optional ``env`` field is an opt-in shim for
    clients that won't issue a request without a local credential — when
    present, the synthetic placeholder is injected into that env var.

    :param model: The validated ``https_bearer`` entry; carries
        ``target``/``targets`` and an optional ``env`` (the sandbox env
        var that receives the synthetic placeholder).
    :param source: Parsed credential source shared by every host binding.
    :param index: Entry index for error messages.
    :returns: One :class:`CredentialProxyEntry` per declared host, each
        wiring ``Authorization: Bearer <real>`` upstream.
    :raises OmnigentError: If a host fails DNS-safety validation.
    """
    inject_env = [model.env] if model.env is not None else []
    return [
        CredentialProxyEntry(
            host=host,
            scheme="bearer",
            source=source,
            inject_env=inject_env,
        )
        for host in _resolve_credential_hosts(model, index=index)
    ]

def _normalize_https_basic(
    model: _CredentialProxyItemModel,
    *,
    source: CredentialSourceSpec,
    index: int,
) -> list[CredentialProxyEntry]:
    """
    Normalize an ``https_basic`` entry into per-host Basic bindings.

    Like ``https_bearer`` this defaults to swap-on-access; ``env`` is an
    optional opt-in injection shim.

    :param model: The validated ``https_basic`` entry; carries
        ``target``/``targets`` with optional ``env`` and optional
        ``username`` (defaults to ``x-access-token``).
    :param source: Parsed credential source shared by every host binding.
    :param index: Entry index for error messages.
    :returns: One :class:`CredentialProxyEntry` per declared host, each
        wiring ``Authorization: Basic b64(username:<real>)`` upstream.
    :raises OmnigentError: If a host fails DNS-safety validation.
    """
    inject_env = [model.env] if model.env is not None else []
    username = model.username or DEFAULT_BASIC_USERNAME
    return [
        CredentialProxyEntry(
            host=host,
            scheme="basic",
            source=source,
            username=username,
            inject_env=inject_env,
        )
        for host in _resolve_credential_hosts(model, index=index)
    ]

def _normalize_git_https(
    model: _CredentialProxyItemModel,
    *,
    source: CredentialSourceSpec,
    index: int,
) -> list[CredentialProxyEntry]:
    """
    Normalize a ``git_https`` entry into per-host Basic bindings.

    Git over HTTPS works purely via swap-on-access: git fires its
    unauthenticated request and the proxy injects ``Basic
    b64(username:<real>)`` for the bound host before it leaves. No env
    var, no in-sandbox git credential helper, nothing credential-shaped
    in the sandbox. (It is the ``https_basic`` primitive with a git-
    friendly default username and no ``env`` shim.)

    :param model: The validated ``git_https`` entry; carries
        ``target``/``targets`` with optional ``username`` (defaults to
        ``x-access-token``).
    :param source: Parsed credential source shared by every host binding.
    :param index: Entry index for error messages.
    :returns: One :class:`CredentialProxyEntry` per declared host (Basic
        upstream, swap-on-access).
    :raises OmnigentError: If a host fails DNS-safety validation.
    """
    username = model.username or DEFAULT_BASIC_USERNAME
    return [
        CredentialProxyEntry(
            host=host,
            scheme="basic",
            source=source,
            username=username,
        )
        for host in _resolve_credential_hosts(model, index=index)
    ]

def _normalize_gh_basic(
    model: _CredentialProxyItemModel,
    *,
    source: CredentialSourceSpec,
    index: int,
) -> list[CredentialProxyEntry]:
    """
    Normalize a ``gh_basic`` entry into git + API credential bindings.

    The git host (anything not prefixed ``api.``) authenticates via
    swap-on-access (Basic), exactly like ``git_https``. The API host
    (prefixed ``api.``, e.g. ``api.github.com``) keeps ``GH_TOKEN`` /
    ``GITHUB_TOKEN`` injection (the ``token`` scheme): ``gh`` refuses to
    issue an API request unless it sees a token locally, so the synthetic
    placeholder is injected to make it emit a request the proxy can swap.

    :param model: The validated ``gh_basic`` entry; ``target``/``targets``
        are optional and default to ``github.com`` + ``api.github.com``.
    :param source: Parsed credential source shared by both bindings.
    :param index: Entry index for error messages.
    :returns: One or two :class:`CredentialProxyEntry` bindings.
    :raises OmnigentError: If an explicit host fails DNS-safety validation.
    """
    if model.target is not None or model.targets is not None:
        hosts = _resolve_credential_hosts(model, index=index)
    else:
        hosts = list(_GH_BASIC_DEFAULT_TARGETS)
    entries: list[CredentialProxyEntry] = []
    for host in hosts:
        if host.startswith("api."):
            entries.append(
                CredentialProxyEntry(
                    host=host,
                    scheme="token",
                    source=source,
                    inject_env=list(_GH_TOKEN_ENV_VARS),
                )
            )
        else:
            entries.append(
                CredentialProxyEntry(
                    host=host,
                    scheme="basic",
                    source=source,
                    username=DEFAULT_BASIC_USERNAME,
                )
            )
    return entries

def _resolve_credential_hosts(model: _CredentialProxyItemModel, *, index: int) -> list[str]:
    """
    Resolve a validated entry's ``target`` / ``targets`` into bound hosts.

    Cardinality (exactly one of ``target`` / ``targets`` for the
    ``https_*`` / ``git_https`` types; at most one for ``gh_basic``) is
    already enforced by :class:`_CredentialProxyItemModel`; this only
    splits each ``host`` / ``host/path`` value and validates the host
    against the DNS grammar. Only the host component binds the credential
    — path scoping is enforced by ``egress_rules``.

    :param model: The validated entry model. Exactly one of ``target`` /
        ``targets`` is set when this is called.
    :param index: Entry index for error messages.
    :returns: De-duplicated, order-preserving list of lower-cased hosts.
    :raises OmnigentError: If a host fails DNS-safety validation.
    """
    if model.target is not None:
        raw_targets = [model.target]
        field_paths = [f"os_env.sandbox.credential_proxy[{index}].target"]
    else:
        # The model validator guarantees ``targets`` is a non-empty list
        # whenever ``target`` is absent for the types that reach here.
        assert model.targets is not None
        raw_targets = model.targets
        field_paths = [
            f"os_env.sandbox.credential_proxy[{index}].targets[{j}]"
            for j in range(len(model.targets))
        ]
    hosts: list[str] = []
    for raw_target, field_path in zip(raw_targets, field_paths, strict=True):
        host = _parse_credential_proxy_host(raw_target, field_path=field_path)
        if host not in hosts:
            hosts.append(host)
    return hosts

def _parse_credential_proxy_host(raw: str, *, field_path: str) -> str:
    """
    Parse one ``host`` or ``host/path`` target into a validated host.

    :param raw: Raw target value, e.g. ``"github.com/org/repo.git"`` or
        ``"api.github.com"``.
    :param field_path: Human-readable path for parse errors.
    :returns: The lower-cased host component.
    :raises OmnigentError: If the value is empty or the host contains
        characters outside the DNS grammar ``[A-Za-z0-9.-]`` (wildcards
        included — credentials bind to an exact host).
    """
    from omnigent.inner.egress.rules import is_dns_safe_host

    if not raw.strip():
        raise OmnigentError(
            f"{field_path} must be a non-empty string (host or host/path), got {raw!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    host = raw.strip().split("/", 1)[0].lower()
    if not is_dns_safe_host(host):
        raise OmnigentError(
            f"{field_path} host {host!r} must be an exact DNS hostname "
            "(letters/digits/dot/hyphen, no wildcards)",
            code=ErrorCode.INVALID_INPUT,
        )
    return host


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _capabilities as _sib_capabilities
    from . import _core as _sib_core
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
    for _key, _value in _sib_core.__dict__.items():
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
