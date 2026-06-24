"""User identity extraction from incoming requests.

Provides a pluggable :class:`AuthProvider` ABC and a
:class:`UnifiedAuthProvider` that supports three identity sources,
selected via the ``OMNIGENT_AUTH_PROVIDER`` env var:

- ``"header"`` (default): reads ``X-Forwarded-Email`` header from
  a trusted upstream proxy. Requests without the header are
  rejected (401) unless the server was explicitly started as a
  single-user local runtime (``OMNIGENT_LOCAL_SINGLE_USER=1``),
  in which case they fall back to the reserved ``"local"`` user.
- ``"oidc"``: reads the ``__Host-ap_session`` signed cookie minted
  after a full OIDC authorization-code+PKCE login flow.
- ``"accounts"``: same signed cookie machinery as OIDC, but minted
  by the built-in username+password ``/auth/login`` endpoint. The
  ``accounts`` provider is the OSS-CUJ-v2 default — first-user-is-admin
  with invite-only signup; see ``designs/oss-cuj/04-implementation-plan.md``.

Cookie validation is identical across OIDC and accounts modes —
both share :class:`AccountsConfig`/:class:`OIDCConfig`-shaped cookie
parameters. The provider is instantiated once at server startup
and closed over by route factories — no per-request import cost.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Protocol

from starlette.requests import HTTPConnection

from omnigent.server.principal import Principal

logger = logging.getLogger(__name__)

# Opt-in multi-user switch. ``OMNIGENT_AUTH_ENABLED`` is the current
# name; ``OMNIGENT_ACCOUNTS_ENABLED`` is the pre-rename name, still
# honored as a deprecated alias (see :func:`_auth_enabled`).
_AUTH_ENABLED_ENV = "OMNIGENT_AUTH_ENABLED"
_AUTH_ENABLED_ENV_DEPRECATED = "OMNIGENT_ACCOUNTS_ENABLED"

RESERVED_USER_LOCAL = "local"
RESERVED_USER_PUBLIC = "__public__"
_RESERVED_USERS = frozenset({RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC})
_TRUTHY_STRINGS = ("1", "true", "yes")

# Explicit single-user marker. Set by the managed local-server spawn
# paths (`omnigent run` in chat.py, the daemon's
# host/local_server.py) and by the canonical bare loopback
# `omnigent server` (cli.py) — never by deployed multi-user servers.
# Gates the header-mode "local" fallback (see
# :meth:`UnifiedAuthProvider._check_header`) and host_id re-owning in
# routes/host_tunnel.py.
_LOCAL_SINGLE_USER_ENV = "OMNIGENT_LOCAL_SINGLE_USER"

LEVEL_READ = 1
LEVEL_EDIT = 2
LEVEL_MANAGE = 3
LEVEL_OWNER = 4


def env_var_is_truthy(name: str, *, default: bool = False) -> bool:
    """Parse a boolean-style environment variable.

    Truthy values match the existing harness env-var convention:
    ``"1"``, ``"true"``, and ``"yes"`` are true
    case-insensitively. Unset or empty values return ``default``;
    every other value is false.

    :param name: Environment variable name.
    :param default: Value to return when the variable is unset or
        empty.
    :returns: Parsed boolean value.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY_STRINGS


def local_single_user_enabled() -> bool:
    """Whether this server is an explicit single-user local runtime.

    Reads ``OMNIGENT_LOCAL_SINGLE_USER``, the marker the managed
    local spawn paths set when starting THE user's own loopback
    server. Deployed multi-user servers never set it, so everything
    it gates (header-mode ``"local"`` fallback, host_id re-owning)
    stays fail-closed there.

    :returns: ``True`` when the single-user marker is set and truthy.
    """
    return env_var_is_truthy(_LOCAL_SINGLE_USER_ENV)


_auth_enabled_deprecation_warned = False


def _auth_enabled() -> bool:
    """Whether multi-user auth is opted in via the enable switch.

    Reads ``OMNIGENT_AUTH_ENABLED``. The pre-rename name
    ``OMNIGENT_ACCOUNTS_ENABLED`` is still honored as a deprecated
    alias: when it is set and the current name is not, its value is
    used and a one-time deprecation warning is logged. The current name
    always wins when both are set, so a deploy migrating to the new name
    can leave the old one in place without surprise.

    Both names share the same truthiness rules (see
    :func:`env_var_is_truthy`) and the same explicit-falsy kill-switch
    semantics — ``OMNIGENT_AUTH_ENABLED=0`` disables auth even though
    the var is "set", which is how the Docker entrypoint lets an
    operator opt back out of the default-on accounts mode.

    :returns: ``True`` when multi-user auth should be enabled.
    """
    global _auth_enabled_deprecation_warned
    if os.environ.get(_AUTH_ENABLED_ENV, "").strip():
        return env_var_is_truthy(_AUTH_ENABLED_ENV, default=False)
    if os.environ.get(_AUTH_ENABLED_ENV_DEPRECATED, "").strip():
        if not _auth_enabled_deprecation_warned:
            logger.warning(
                "%s is deprecated; rename it to %s. The old name still "
                "works for now but will be removed in a future release.",
                _AUTH_ENABLED_ENV_DEPRECATED,
                _AUTH_ENABLED_ENV,
            )
            _auth_enabled_deprecation_warned = True
        return env_var_is_truthy(_AUTH_ENABLED_ENV_DEPRECATED, default=False)
    return False


def resolve_auth_source() -> str:
    """
    Resolve the server's auth provider source from the environment.

    Single source of truth for the auth-mode decision so every spawn
    path (``create_auth_provider`` here, the daemon-owned local server in
    ``host/local_server.py``, and the per-command server in ``chat.py``)
    agrees on which mode a server boots in. The rules mirror
    :func:`create_auth_provider`:

    - An explicit ``OMNIGENT_AUTH_PROVIDER`` (case-insensitive) always
      wins, e.g. ``"header"`` / ``"oidc"`` / ``"accounts"``. This is the
      low-level escape hatch.
    - Otherwise ``header`` is the default, unless the opt-in switch
      ``OMNIGENT_AUTH_ENABLED`` is truthy (see :func:`_auth_enabled`,
      which also honors the deprecated ``OMNIGENT_ACCOUNTS_ENABLED``
      alias). When enabled, the mode depends on whether OIDC config was
      supplied:

      - ``OMNIGENT_OIDC_ISSUER`` is set → ``"oidc"`` (the operator
        brought their own IdP). The issuer is the canonical, always-
        required OIDC identifier; :func:`OIDCConfig.from_env` then fails
        loud if the rest of the OIDC config is missing.
      - otherwise → ``"accounts"`` (the built-in username+password
        login flow).

    :returns: The resolved source string, e.g. ``"accounts"``,
        ``"header"``, or ``"oidc"`` (or any explicit lower-cased value of
        ``OMNIGENT_AUTH_PROVIDER``). The caller is responsible for
        rejecting unknown values.
    """
    raw_source = os.environ.get("OMNIGENT_AUTH_PROVIDER")
    if raw_source and raw_source.strip():
        return raw_source.strip().lower()
    # Opt-in multi-user — see create_auth_provider's docstring.
    if _auth_enabled():
        # An operator-supplied OIDC issuer selects the native
        # authorization-code flow; otherwise the built-in accounts flow.
        if os.environ.get("OMNIGENT_OIDC_ISSUER", "").strip():
            return "oidc"
        return "accounts"
    return "header"


class AuthProvider(ABC):
    """Extract a user ID from an incoming request.

    Implementations must return a user ID string or ``None``.
    When ``None`` is returned, the route helpers respond with 401.
    """

    @abstractmethod
    def get_user_id(self, request: HTTPConnection) -> str | None:
        """Return the authenticated user ID, or ``None``."""
        ...

    def get_principal(self, request: HTTPConnection) -> Principal | None:
        """Return the resolved :class:`Principal`, or ``None`` (Adapter).

        Concrete default: adapt :meth:`get_user_id` into a ``Principal``
        carrying only ``user_id``. Every existing provider gains
        ``get_principal`` for free with behavior identical to ``get_user_id``
        — ``None`` stays ``None``, the ``"local"`` fallback still wraps into
        ``Principal(user_id="local")``. A resolver that can produce richer
        identity (tenant/roles/claims) overrides this; nothing in this
        increment does.

        :param request: The incoming HTTP request or WebSocket handshake.
        :returns: ``Principal(user_id=...)`` when ``get_user_id`` resolves an
            identity; ``None`` otherwise.
        """
        uid = self.get_user_id(request)
        return Principal(user_id=uid) if uid is not None else None


class CompositeAuthProvider(AuthProvider):
    """Chain-of-Responsibility over interchangeable identity resolvers.

    Each contributed resolver is itself an :class:`AuthProvider` (Strategy):
    on every request the composite tries the resolvers in order and returns the
    first non-``None`` result, falling through to the configured base provider
    last. ``get_principal`` and ``get_user_id`` both resolve through the same
    chain so the two stay consistent.

    Extension-contributed resolvers (from
    :func:`omnigent.extensions.extension_principal_resolvers`) are placed
    BEFORE the configured provider so an external consumer (e.g. the platform
    supplying identity) can win when it has an answer, while an in-core deploy
    with no resolvers is behavior-identical to the base provider alone.

    ``tail_resolvers`` run strictly AFTER the configured base, so they are a
    fall-through for system callers that carry no user identity — e.g. the
    runner-token resolver (BDP-2437), which a request only ever clears when it
    presents a server-issued binding token bound to a launched runner. A real
    user cookie resolves on the base FIRST, so a tail resolver can never shadow
    it. Crucially, the configured provider stays in the ``_base`` slot so
    :func:`unwrap_auth_base` / :func:`accounts_provider` still see it — a tail
    resolver must never occupy ``_base``.

    :param base: The configured :class:`AuthProvider` consulted between the
        head resolvers and the tail resolvers. ``None`` is rejected — a
        composite always wraps a real base.
    :param resolvers: Extra resolvers tried, in order, before ``base``.
        Defaults to empty (zero behavior change).
    :param tail_resolvers: Extra resolvers tried, in order, after ``base``.
        Defaults to empty.
    """

    def __init__(
        self,
        base: AuthProvider,
        resolvers: list[AuthProvider] | None = None,
        tail_resolvers: list[AuthProvider] | None = None,
    ) -> None:
        if base is None:
            raise ValueError("CompositeAuthProvider requires a base AuthProvider")
        # Head resolvers first, configured base next, tail resolvers last.
        self._chain: tuple[AuthProvider, ...] = (
            *(resolvers or ()),
            base,
            *(tail_resolvers or ()),
        )
        self._base = base

    def get_principal(self, request: HTTPConnection) -> Principal | None:
        """Return the first non-``None`` principal in the chain, else ``None``."""
        for resolver in self._chain:
            principal = resolver.get_principal(request)
            if principal is not None:
                return principal
        return None

    def get_user_id(self, request: HTTPConnection) -> str | None:
        """Resolve identity through the chain, returning the principal's user id.

        Defers to :meth:`get_principal` so the chain order governs both
        accessors identically.
        """
        principal = self.get_principal(request)
        return principal.user_id if principal is not None else None


class UnifiedAuthProvider(AuthProvider):
    """Unified authentication provider that supports header-based,
    OIDC, and accounts cookie-based identity extraction.

    Exactly one source is active per deployment, selected by
    ``OMNIGENT_AUTH_PROVIDER``. OIDC and accounts modes share
    the same cookie machinery — the difference is only in how the
    cookie was minted (OIDC IdP callback vs ``/auth/login``).

    :param source: The active identity source: ``"header"``,
        ``"oidc"``, or ``"accounts"``.
    :param oidc_config: OIDC configuration. Required when
        ``source`` is ``"oidc"``, ``None`` otherwise.
    :param accounts_config: Accounts configuration. Required when
        ``source`` is ``"accounts"``, ``None`` otherwise.
    :param local_single_user: When ``True``, header mode falls back
        to the reserved ``"local"`` identity for requests without
        ``X-Forwarded-Email`` — the explicit single-user posture of
        the user's own loopback server. When ``False``, such
        requests are rejected (``None`` → 401, fail closed).
        ``None`` (the default) resolves from
        ``OMNIGENT_LOCAL_SINGLE_USER`` at construction (see
        :func:`local_single_user_enabled`). Only consulted in
        header mode. Tests pass an explicit bool.
    """

    def __init__(
        self,
        source: str,
        oidc_config: OIDCConfig | None = None,
        accounts_config: AccountsConfig | None = None,
        local_single_user: bool | None = None,
    ) -> None:
        self._source = source
        self._oidc_config = oidc_config
        self._accounts_config = accounts_config
        self._local_single_user = (
            local_single_user if local_single_user is not None else local_single_user_enabled()
        )
        self._cookie_cache: dict[str, tuple[str, float]] = {}

    @property
    def login_url(self) -> str | None:
        """Where the frontend should redirect on 401.

        - ``"oidc"`` → ``"/auth/login"`` (server-side GET that
          builds the PKCE state cookie and redirects to the IdP's
          authorize endpoint).
        - ``"accounts"`` → ``"/login"`` (SPA route — the React
          ``LoginPage`` renders a username + password form and
          POSTs to ``/auth/login``). Distinct from OIDC because
          accounts mode has no IdP handoff; the form lives in the
          browser.
        - ``"header"`` → ``None`` (no login page; missing identity
          is the proxy's responsibility).
        """
        if self._source == "oidc":
            return "/auth/login"
        if self._source == "accounts":
            return "/login"
        return None

    def get_user_id(self, request: HTTPConnection) -> str | None:
        """Extract user identity from the active source.

        - ``"header"``: Read ``X-Forwarded-Email`` header.
        - ``"oidc"`` / ``"accounts"``: Read ``__Host-ap_session``
          cookie, validate HS256 signature and expiry, return
          ``sub`` claim.

        :param request: The incoming HTTP request or WebSocket
            handshake (both are ``HTTPConnection``).
        :returns: Authenticated user ID, or ``None`` (→ 401).
        """
        if self._source in ("oidc", "accounts"):
            return self._check_cookie(request)
        return self._check_header(request)

    def _check_cookie(self, request: HTTPConnection) -> str | None:
        """Validate the session cookie or Bearer token and return the
        user ID.

        Checks the session cookie first (browser clients), then
        falls back to ``Authorization: Bearer <jwt>`` (CLI clients
        authenticated via ``omnigent login``). Both carry the same
        HS256-signed JWT.

        Uses a TTL credential cache keyed by HMAC-SHA256 digest of
        the raw token to avoid repeated JWT decoding on every
        request.

        :param request: The incoming HTTP request or WebSocket.
        :returns: User ID from the JWT's ``sub`` claim, or
            ``None`` if no valid token is found.
        """
        import jwt

        from omnigent.server.oidc import hmac_digest

        # Both OIDC and accounts modes use the same cookie machinery
        # — read the active config wherever it lives. The two configs
        # share `cookie_secret` and `session_cookie_name` properties
        # by construction (see AccountsConfig docstring).
        cookie_config = self._oidc_config if self._source == "oidc" else self._accounts_config
        cookie_name = cookie_config.session_cookie_name
        token = request.cookies.get(cookie_name)
        if not token:
            # Fall back to Bearer token for CLI clients.
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
        if not token:
            return None

        cache_key = hmac_digest(token, cookie_config.cookie_secret)
        cached = self._cookie_cache.get(cache_key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]

        try:
            payload = jwt.decode(
                token,
                cookie_config.cookie_secret,
                algorithms=["HS256"],
            )
        except jwt.InvalidTokenError:
            return None

        user_id = payload.get("sub")
        if not user_id or user_id in _RESERVED_USERS:
            return None

        # Cache for remaining lifetime of the token.
        remaining = payload.get("exp", 0) - time.time()
        if remaining > 0:
            self._cookie_cache[cache_key] = (
                user_id,
                time.monotonic() + remaining,
            )

        return user_id

    def _check_header(self, request: HTTPConnection) -> str | None:
        """Read the ``X-Forwarded-Email`` header and return the user ID.

        When the header is present, its value is used as the identity
        (reserved names like ``"local"`` are rejected). When absent,
        the request is rejected (``None`` → 401): a missing or
        dropped proxy header must fail closed, never resolve to a
        shared default identity that every unauthenticated request
        would then share.

        The one exception is the explicit single-user local runtime
        (``local_single_user=True``, from
        ``OMNIGENT_LOCAL_SINGLE_USER=1``): there the absent header
        falls back to :data:`RESERVED_USER_LOCAL`, because the
        server's only user IS the local user and no proxy exists to
        inject identity.

        :param request: The incoming HTTP request or WebSocket.
        :returns: User ID from the header; ``"local"`` when the
            header is absent on a single-user local runtime; else
            ``None`` (→ 401).
        """
        email = request.headers.get("X-Forwarded-Email")
        if email:
            if email in _RESERVED_USERS:
                return None
            return email
        if self._local_single_user:
            return RESERVED_USER_LOCAL
        return None


class _LaunchOwnerRegistry(Protocol):
    """The slice of :class:`TunnelRegistry` the runner-token provider needs.

    Typed as a :class:`Protocol` so :class:`RunnerTokenAuthProvider` does not
    import the runner transport stack at module load and stays unit-testable
    with a stub.
    """

    def launch_owner(self, runner_id: str) -> str | None: ...


class RunnerTokenAuthProvider(AuthProvider):
    """Resolve a runner's identity from its server-issued binding token (BDP-2437).

    A runner spawned on a host pod authenticates its HTTP callbacks
    (``GET /v1/sessions/{id}/agent/contents``, ``/items``,
    ``/v1/sessions/{id}``) with the server-issued tunnel binding token, but
    presents no user cookie. Under accounts mode the cookie-based provider then
    yields no identity and the callbacks 401, failing every turn with
    ``spec_resolver_failed``. This resolver is the symmetric HTTP-side mirror of
    the BDP-2436 WS-tunnel fix: it derives the runner id from the token and
    resolves the owner from the TRUSTED launch record the server stored when it
    launched that runner id for a session.

    Security invariant (identical to BDP-2436): a runner authenticates ONLY as
    the owner it was launched for. ``runner_id = token_bound_runner_id(token)``;
    ``owner = registry.launch_owner(runner_id)``. A token/runner_id the server
    never launched (attacker-chosen/forged) has NO launch record, so ``owner``
    is ``None`` and this resolver does not authenticate it — the composite falls
    through and the request is rejected (401). The token alone never grants
    access.

    The token is read ONLY from the ``X-Omnigent-Runner-Tunnel-Token`` header
    (the existing first-party runner→server scheme — see the WS tunnel and
    ``cost_advisor._runner_identity_headers``). It is deliberately NOT read from
    ``Authorization: Bearer``, which the accounts provider owns for session
    JWTs.

    Wired as a ``tail_resolver`` of :class:`CompositeAuthProvider` so it runs
    strictly after the user-cookie base — it can never shadow a real logged-in
    user.

    :param registry: The server's runner tunnel registry. Read live on every
        request so launch-record eviction / relaunch is reflected; the owner is
        never cached here.
    """

    def __init__(self, registry: _LaunchOwnerRegistry) -> None:
        self._registry = registry
        # token → runner_id memo. Only the deterministic derivation is cached;
        # the owner is always re-read from the registry below.
        self._runner_id_cache: dict[str, str] = {}

    def get_user_id(self, request: HTTPConnection) -> str | None:
        """Return the launch owner for a valid runner token, else ``None``.

        :param request: Incoming HTTP request or WebSocket handshake.
        :returns: The owner the runner id was launched for, or ``None`` when no
            (valid, launched) runner token is present.
        """
        from omnigent.runner.identity import (
            RUNNER_TUNNEL_TOKEN_HEADER,
            token_bound_runner_id,
        )

        token = request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
        if not token:
            return None
        stripped = token.strip()
        if not stripped:
            return None
        runner_id = self._runner_id_cache.get(stripped)
        if runner_id is None:
            runner_id = token_bound_runner_id(stripped)
            self._runner_id_cache[stripped] = runner_id
        # Always live: a forged token has no launch record → None → 401.
        return self._registry.launch_owner(runner_id)


def unwrap_auth_base(provider: AuthProvider | None) -> AuthProvider | None:
    """The configured provider beneath a principal-resolver wrap (BDP-2426).

    The BDP-2388 :class:`CompositeAuthProvider` wraps the configured provider
    for request-time identity resolution whenever an extension contributes a
    resolver (the bytedesk extension always does). Build-time mode detection —
    the accounts source, the OIDC config, and the login URL the SPA redirects to
    on 401 — must look *through* that wrap to the configured base, or it sees the
    composite (which exposes none of those). Non-composite providers, and
    ``None``, pass through unchanged.

    :param provider: The (possibly composite-wrapped) configured provider.
    :returns: The underlying configured provider (``._base``) when wrapped, else
        ``provider`` itself.
    """
    if isinstance(provider, CompositeAuthProvider):
        return provider._base
    return provider


def accounts_provider(provider: AuthProvider | None) -> UnifiedAuthProvider | None:
    """The active accounts :class:`UnifiedAuthProvider`, seen through a wrap.

    A bare ``isinstance(provider, UnifiedAuthProvider)`` no longer recognizes
    accounts mode once the principal-resolver :class:`CompositeAuthProvider`
    wraps it, so this unwraps (:func:`unwrap_auth_base`) before the source check.
    Keeps the accounts bootstrap, the ``/auth`` router, and the ``/v1/info``
    ``accounts_enabled`` flag working under a principal resolver (BDP-2426).

    :param provider: The (possibly composite-wrapped) configured provider.
    :returns: The underlying accounts ``UnifiedAuthProvider``, or ``None`` when
        the deployment is not in accounts mode.
    """
    base = unwrap_auth_base(provider)
    if isinstance(base, UnifiedAuthProvider) and base._source == "accounts":
        return base
    return None


def create_auth_provider() -> AuthProvider:
    """Factory: read ``OMNIGENT_AUTH_PROVIDER`` and return a
    :class:`UnifiedAuthProvider` configured for the selected source.

    Defaults to ``"header"`` when the env var is unset — a bare
    ``omnigent server`` is single-user, no-login out of the box.
    Header mode rejects requests without ``X-Forwarded-Email``
    (401, fail closed — see :meth:`UnifiedAuthProvider._check_header`)
    unless the server is an explicit single-user local runtime
    (``OMNIGENT_LOCAL_SINGLE_USER=1``, set by the managed local
    spawn paths and the canonical bare loopback ``omnigent
    server``), where the absent header falls back to the reserved
    ``"local"`` user — the convenient posture for local development
    without minting cookies / typing passwords.

    Opt-in multi-user (accounts / OIDC)
    -----------------------------------
    Set ``OMNIGENT_AUTH_ENABLED=1`` (or any truthy value) to turn on
    multi-user auth. With no OIDC config present this selects
    ``accounts`` mode — the built-in login flow with
    first-user-is-admin setup. Set the ``OMNIGENT_OIDC_*`` env vars
    (at minimum ``OMNIGENT_OIDC_ISSUER``) alongside it and the same
    switch instead selects ``oidc`` — the native authorization-code
    flow against your own IdP. Containerized / remote deploys (Docker,
    HF Spaces, Render, Railway) flip this on in their entrypoints so a
    deployed instance is authenticated by default; a bare local server
    leaves it off. An explicit ``OMNIGENT_AUTH_PROVIDER`` always wins
    over this switch — it only governs the env-unset default. Deploys
    behind an SSO proxy that injects ``X-Forwarded-Email`` set
    ``OMNIGENT_AUTH_PROVIDER=header`` (Databricks Apps, oauth2-proxy).

    (``OMNIGENT_AUTH_ENABLED`` is the renamed opt-in gate,
    commit ``b23e886e``, formerly ``OMNIGENT_ACCOUNTS_ENABLED``:
    header is the shipped default, so the var is an enable switch, not
    a kill switch. The old name is still honored as a deprecated
    alias — see :func:`_auth_enabled`.)

    Validates the source's required env vars at startup (fail
    loud) — OIDC fetches the discovery document, accounts decodes
    the cookie secret.

    :returns: Configured auth provider.
    :raises RuntimeError: On unknown source or invalid config.
    """
    source = resolve_auth_source()

    if source not in ("header", "oidc", "accounts"):
        raise RuntimeError(
            f"Unknown OMNIGENT_AUTH_PROVIDER={source!r}. Valid: 'header', 'oidc', 'accounts'"
        )

    oidc_config: OIDCConfig | None = None
    accounts_config: AccountsConfig | None = None
    if source == "oidc":
        from omnigent.server.oidc import OIDCConfig

        oidc_config = OIDCConfig.from_env()
    elif source == "accounts":
        # Reaching here means accounts mode was deliberately selected
        # — either OMNIGENT_AUTH_PROVIDER=accounts or the
        # OMNIGENT_AUTH_ENABLED=1 opt-in without OIDC config
        # (resolved above). No second gate: the selection already
        # expressed intent.
        from omnigent.server.accounts_config import AccountsConfig

        accounts_config = AccountsConfig.from_env()

    return UnifiedAuthProvider(
        source=source,
        oidc_config=oidc_config,
        accounts_config=accounts_config,
    )


# Backwards-compatible re-export of forward-referenced config
# types — both are imported lazily inside `create_auth_provider`
# to keep startup cost off the import path that doesn't use them.
if False:  # TYPE_CHECKING equivalent without the import
    from omnigent.server.accounts_config import AccountsConfig
    from omnigent.server.oidc import OIDCConfig
