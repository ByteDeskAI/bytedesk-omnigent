"""Generic first-party extension seam (ADR-0143).

The single, generic mechanism that lets out-of-core packages add functionality to
the omnigent server without editing the core app factory: extensions are
discovered via the ``omnigent.extensions`` setuptools entry-point group and each
one's routers are mounted. This module is deliberately **generic** (no reference
to any specific extension) and is the one upstream-contributable seam; all
first-party feature code lives in its own package (e.g. ``bytedesk_omnigent``).

``create_app`` calls :func:`install_extensions` once. The discovery vs. install
split keeps the install logic unit-testable with injected fakes (no installed
entry-point metadata required).
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Awaitable, Callable
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Annotation-only (deferred by `from __future__ import annotations`).
    # Kept out of the runtime import graph so importing this discovery hub —
    # and therefore any PluggableRegistry that discovers extensions — does NOT
    # drag in the ~100ms FastAPI stack on the runner hot path.
    from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)

#: setuptools entry-point group extensions register under.
ENTRY_POINT_GROUP = "omnigent.extensions"
#: Explicit ``module:factory`` registrations (comma-separated), checked in addition
#: to entry-points. Lets source-mounted local-dev pods load extensions without the
#: image's baked entry_points.txt being regenerated (ADR-0143 / BDP-2294).
ENV_VAR = "OMNIGENT_EXTENSIONS"
#: Comma-separated extension *names* to disable (the ``EnableFeatures`` analog,
#: ADR-0143 §4.5 / BDP-2504). Filtered inside :func:`discover_extensions`; unset
#: (the default) is a no-op. Disables an extension without removing its package
#: or editing entry-points.
DISABLED_ENV_VAR = "OMNIGENT_DISABLED_EXTENSIONS"


@runtime_checkable
class OmnigentExtension(Protocol):
    """An out-of-core extension the server discovers and installs (ADR-0143).

    ``name`` and ``routers`` are required. The capability methods below
    (``tool_factories`` / ``policy_modules`` / ``secret_backends`` /
    ``background_tasks`` / …) are **optional** — an extension contributes only
    the surfaces it has. They are declared here for the type checker; the
    aggregators use ``hasattr`` so an extension that omits one is simply skipped
    (no ``getattr`` default probe).

    The staged lifecycle hooks (``pre_init`` / ``post_init`` / ``after_init``)
    and the ``tool_interceptors`` hook are **not** declared on this
    ``@runtime_checkable`` Protocol — see :class:`OmnigentExtensionLifecycle`
    below for why. They are equally optional and ``hasattr``-probed.
    """

    name: str

    def routers(
        self,
        auth_provider: object | None = ...,
        permission_store: object | None = ...,
    ) -> list[APIRouter]:
        """FastAPI routers to mount under ``/v1`` (empty list if none).

        ``auth_provider`` is passed by :func:`install_extensions` for routes
        that need it. ``permission_store`` is passed for admin-gated extension
        routes that need the same server-wide admin flag as core routes. An
        extension whose ``routers`` takes fewer arguments is still accepted
        (back-compat, handled by the install-time ``TypeError`` retry).
        """
        ...

    # ── optional capability methods ──────────────────────────────────
    def tool_factories(self) -> dict[str, Callable[[object], object]]:
        """Builtin tool factories (``{name: factory(config) -> Tool}``)."""
        ...

    def policy_modules(self) -> list[str]:
        """Policy-builtin module import paths the policy registry scans."""
        ...

    def secret_backends(self) -> list[object]:
        """Secret backends consulted by :mod:`omnigent.onboarding.secrets`."""
        ...

    def default_mcp_servers(self) -> list[object]:
        """``MCPServerConfig``s merged into EVERY agent spec at load (BDP-2459).

        Lets an extension expose a platform-wide tool to all agents from one place
        — e.g. ``bytedesk_omnigent``'s shared-memory stdio MCP front — without
        editing each bundle's ``config.yaml``. Merged by name in
        :func:`omnigent.spec.load` (a spec's own server of the same name wins).
        Defaults to ``[]`` via ``hasattr`` probing.
        """
        ...

    def background_tasks(self) -> list[Callable[[], Awaitable[None]]]:
        """Background-task factories the server lifespan starts and cancels."""
        ...

    def config_descriptors(self) -> list[object]:
        """Configuration-Control-Plane descriptors (ADR-0150, BDP-2413).

        Each is a :class:`omnigent.config.ConfigDescriptor` registered into the
        Settings Registry, so an extension's configurable properties are
        auto-exposed through the uniform ``/v1/config`` REST surface. Defaults
        to ``[]`` via ``hasattr`` probing — an extension that omits it adds no
        config keys.
        """
        ...

    def principal_resolvers(self) -> list[object]:
        """Identity resolvers contributed to the request principal chain.

        Each is an :class:`omnigent.server.auth.AuthProvider`; the server wraps
        the configured provider in a ``CompositeAuthProvider`` and tries these
        BEFORE it (Chain of Responsibility). Defaults to ``[]`` via ``hasattr``
        probing, so an extension that omits it is unaffected.
        """
        ...

    # ── identity ports (omnigent/identity/, ADR adr-omnigent-pluggable-identity) ──
    # Each returns ``{name: factory}`` and is discovered via the matching
    # PluggableRegistry seam (omnigent/pluggable/manifest.py SEAMS), so a consumer
    # swaps the trust mechanism / outbound credential / authorization without a
    # core edit. Probed by ``hasattr`` in PluggableRegistry.discover_extensions —
    # an extension that omits any is simply skipped.
    def assertion_verifiers(self) -> dict[str, Callable[[], object]]:
        """Inbound-assertion verifiers ({name: factory}) — the trust subpart."""
        ...

    def outbound_credential_providers(self) -> dict[str, Callable[[], object]]:
        """Outbound credential providers ({name: factory}) — the act-as subpart."""
        ...

    def authorization_providers(self) -> dict[str, Callable[[], object]]:
        """Authorization providers ({name: factory}) — the allow/deny subpart."""
        ...

    # NOTE: the optional staged lifecycle hooks (pre_init / post_init /
    # after_init) and the tool_interceptors hook are deliberately NOT declared
    # on this @runtime_checkable Protocol. A runtime_checkable Protocol requires
    # an instance to carry EVERY declared member to satisfy isinstance(); adding
    # these here would flip isinstance(legacy_extension, OmnigentExtension) from
    # True to False for any extension that doesn't implement all four (BDP-2504
    # regression). They live on OmnigentExtensionLifecycle below — equally
    # optional, hasattr-probed by install_extensions / extension_tool_interceptors,
    # never used in an isinstance() gate.


class OmnigentExtensionLifecycle(Protocol):
    """Optional staged-lifecycle + tool-interception hooks (ADR-0143 §4.3/§5).

    Deliberately a SEPARATE, non-``@runtime_checkable`` Protocol from
    :class:`OmnigentExtension`. These hooks are optional, so they must not be
    members of the runtime-checkable contract — a runtime-checkable Protocol
    requires every declared member to be present for ``isinstance`` to pass,
    which would break legacy extensions that implement none of them (BDP-2504).
    This Protocol exists purely for static typing / documentation; the runtime
    never gates on it — :func:`install_extensions` and
    :func:`extension_tool_interceptors` ``hasattr``-probe each hook by name.

    ServiceStack analogs: ``IPreInitPlugin`` / ``IPostInitPlugin`` /
    ``IAfterInitAppHost``. Each lifecycle hook receives the host (the FastAPI
    ``app``) so it can stash cross-extension state on it.
    """

    def pre_init(self, host: FastAPI) -> None:
        """Stage 1 — called BEFORE any router is mounted.

        Use to create DB tables, validate required env/secrets, or fail fast.
        An exception here aborts ONLY this extension (it is dropped from every
        later stage via the ``healthy`` set) and is logged — it must never kill
        server boot.
        """
        ...

    def post_init(self, host: FastAPI) -> None:
        """Stage 3 — called AFTER all healthy extensions' routers are mounted.

        Use to register cross-extension state or wire inter-extension
        dependencies now that every router/seam contribution is in place.
        """
        ...

    def after_init(self, host: FastAPI) -> None:
        """Stage 4 — called after ``post_init`` for every healthy extension.

        The ``IAfterInitAppHost`` analog: the final settle hook, run once the
        whole extension set is mounted and post-init wired, before the server
        lifespan (background tasks) starts.
        """
        ...

    def tool_interceptors(self) -> dict[str, Callable[..., object]]:
        """Tool-call interceptors keyed by tool-name prefix (``{prefix: handler}``).

        Core consults the prefix table before runner dispatch; a matching
        handler returns a result (or ``None`` to fall through to normal
        dispatch). Lets any extension claim an interception point (e.g.
        ``{"memory__": execute_memory_tool}``) without a hard core→extension
        name reference. Aggregated by :func:`extension_tool_interceptors`;
        defaults to ``{}`` via ``hasattr`` probing.
        """
        ...


def _disabled_from_env() -> set[str]:
    """Extension names disabled via ``OMNIGENT_DISABLED_EXTENSIONS`` (comma-separated).

    The ``EnableFeatures`` analog (ADR-0143 §4.5): an operator disables an entire
    extension by name without removing the package or editing entry-points. The
    empty/unset env var (the default) yields an empty set — a no-op filter.
    """
    raw = os.environ.get(DISABLED_ENV_VAR, "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def _load_env_extensions() -> list[OmnigentExtension]:
    """Load extensions named in the ``OMNIGENT_EXTENSIONS`` env var (``module:factory``)."""
    found: list[OmnigentExtension] = []
    for entry in os.environ.get(ENV_VAR, "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            module_path, _, attr = entry.partition(":")
            factory = getattr(importlib.import_module(module_path), attr)
            found.append(factory())
        except Exception:  # one bad entry must not break server boot
            logger.exception("failed to load %s entry %r", ENV_VAR, entry)
    return found


def discover_extensions(
    *,
    disabled: set[str] | None = None,
) -> list[OmnigentExtension]:
    """Load extensions from the entry-point group + the ``OMNIGENT_EXTENSIONS`` env.

    Entry-points are the production path (registered at install time); the env var
    is the explicit/local-dev path (no installed metadata needed, BDP-2294).
    Deduped by ``name`` so a redundant env entry doesn't double-register. A single
    bad extension is logged and skipped — it must never break server boot.

    :param disabled: extension names to exclude (the ``EnableFeatures`` analog,
        ADR-0143 §4.5 / BDP-2504). Defaults to :func:`_disabled_from_env`
        (``OMNIGENT_DISABLED_EXTENSIONS``); an empty set — the default — filters
        nothing. A disabled extension's factory still runs (entry-point load),
        but it is dropped from the returned set before any stage sees it.
    """
    _disabled = _disabled_from_env() if disabled is None else disabled
    found: list[OmnigentExtension] = []
    seen: set[str] = set()
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            ext = ep.load()()
        except Exception:  # one bad extension must not break server boot
            logger.exception("failed to load omnigent extension %r", ep.name)
            continue
        if ext.name in _disabled:
            logger.info("omnigent extension %r disabled by %s", ext.name, DISABLED_ENV_VAR)
            continue
        found.append(ext)
        seen.add(ext.name)
    for ext in _load_env_extensions():
        if ext.name in _disabled:
            logger.info("omnigent extension %r disabled by %s", ext.name, DISABLED_ENV_VAR)
            continue
        if ext.name not in seen:
            found.append(ext)
            seen.add(ext.name)
    return found


def install_extensions(
    app: FastAPI,
    *,
    extensions: list[OmnigentExtension] | None = None,
    auth_provider: object | None = None,
    permission_store: object | None = None,
) -> list[str]:
    """Mount each extension's routers under ``/v1``; return the installed names.

    :param extensions: defaults to :func:`discover_extensions` (entry-point
        discovery); tests inject fakes directly.
    :param auth_provider: passed to ``ext.routers(auth_provider=...)`` for
        extensions whose routes need it; extensions whose ``routers()`` takes no
        argument are called without it (back-compat).
    :param permission_store: passed to extension routers that need admin or
        permission checks; older extensions are retried without it.
    """
    exts = discover_extensions() if extensions is None else extensions

    # ── Stage 1 — pre_init (ADR-0143 §4.3, BDP-2504) ──────────────────────
    # Optional; ``hasattr``-probed. An extension whose ``pre_init`` raises is
    # marked unhealthy and EXCLUDED from every later stage (§6 risk) — a failed
    # pre-init must never leave that extension's routers mounted. Extensions
    # defining no lifecycle hooks are all healthy and behave exactly as before.
    healthy: set[str] = {ext.name for ext in exts}
    for ext in exts:
        if hasattr(ext, "pre_init"):
            try:
                ext.pre_init(app)
            except Exception:  # one bad pre_init must not break server boot
                logger.exception("extension %r pre_init failed — excluding it", ext.name)
                healthy.discard(ext.name)

    # ── Stage 2 — register (mount routers) ────────────────────────────────
    installed: list[str] = []
    for ext in exts:
        if ext.name not in healthy:
            continue
        try:
            routers = ext.routers(
                auth_provider=auth_provider,
                permission_store=permission_store,
            )
        except TypeError:
            try:
                routers = ext.routers(auth_provider=auth_provider)
            except TypeError:
                routers = ext.routers()
        for router in routers:
            app.include_router(router, prefix="/v1")
        installed.append(ext.name)
        logger.info("installed omnigent extension %r", ext.name)

    # ── Stage 3 — post_init (after all healthy routers mounted) ───────────
    for ext in exts:
        if ext.name not in healthy:
            continue
        if hasattr(ext, "post_init"):
            try:
                ext.post_init(app)
            except Exception:  # observability only — must not break boot
                logger.exception("extension %r post_init failed", ext.name)

    # ── Stage 4 — after_init (final settle, before lifespan starts) ───────
    for ext in exts:
        if ext.name not in healthy:
            continue
        if hasattr(ext, "after_init"):
            try:
                ext.after_init(app)
            except Exception:  # observability only — must not break boot
                logger.exception("extension %r after_init failed", ext.name)

    return installed


def get_extension(name: str) -> OmnigentExtension | None:
    """Return the discovered extension named *name*, or ``None`` if absent (BDP-2504).

    A thin lookup over :func:`discover_extensions` (the ``appHost.GetPlugin<T>()``
    analog). Honors ``OMNIGENT_DISABLED_EXTENSIONS`` — a disabled extension is not
    found. Discovery is re-run per call (not memoized), consistent with the
    aggregators.
    """
    return next((ext for ext in discover_extensions() if ext.name == name), None)


def assert_extension(name: str) -> OmnigentExtension:
    """Like :func:`get_extension` but raise ``LookupError`` if absent (BDP-2504).

    The ``appHost.AssertPlugin<T>()`` analog — for a core path that hard-requires
    a specific extension to be present.
    """
    ext = get_extension(name)
    if ext is None:
        loaded = [e.name for e in discover_extensions()]
        raise LookupError(
            f"required omnigent extension {name!r} not loaded (loaded: {loaded})"
        )
    return ext


def extension_tool_factories() -> dict:
    """Builtin tool factories contributed by extensions (``tool_factories()``).

    Merged into the core ``_BUILTIN_REGISTRY`` so first-party tools register
    without a ByteDesk-specific edit in ``omnigent/tools/builtins/__init__``.
    """
    factories: dict = {}
    for ext in discover_extensions():
        if hasattr(ext, "tool_factories"):
            factories.update(ext.tool_factories())
    return factories


def extension_policy_modules() -> list[str]:
    """Policy-builtin module paths contributed by extensions (``policy_modules()``)."""
    modules: list[str] = []
    for ext in discover_extensions():
        if hasattr(ext, "policy_modules"):
            modules.extend(ext.policy_modules())
    return modules


def extension_secret_backends() -> list:
    """Secret backends contributed by extensions (``secret_backends()``).

    Consulted by :mod:`omnigent.onboarding.secrets` to let an out-of-core package
    (e.g. ``bytedesk_omnigent``'s Infisical backend) take precedence over the
    local keyring/file store while staying upstream-generic here.
    """
    backends: list = []
    for ext in discover_extensions():
        if hasattr(ext, "secret_backends"):
            backends.extend(ext.secret_backends())
    return backends


def extension_default_mcp_servers() -> list:
    """Default MCP-server configs contributed by extensions (``default_mcp_servers()``).

    Merged into every agent spec by :func:`omnigent.spec.load` so a platform-wide
    tool (e.g. shared memory) reaches all agents from one place. Upstream-generic
    here; the concrete servers live in the extension.
    """
    servers: list = []
    for ext in discover_extensions():
        if hasattr(ext, "default_mcp_servers"):
            servers.extend(ext.default_mcp_servers())
    return servers


def extension_principal_resolvers() -> list[object]:
    """Identity resolvers contributed by extensions (``principal_resolvers()``).

    Threaded into :class:`omnigent.server.auth.CompositeAuthProvider` so an
    out-of-core package can supply the request principal (e.g. tenant + roles
    from a gateway header) ahead of the configured provider, while staying
    upstream-generic here. Empty when no extension contributes one.
    """
    resolvers: list[object] = []
    for ext in discover_extensions():
        if hasattr(ext, "principal_resolvers"):
            resolvers.extend(ext.principal_resolvers())
    return resolvers


def extension_background_factories() -> list:
    """Background-task factories contributed by extensions (``background_tasks()``).

    The server lifespan starts each as a task and cancels it on shutdown.
    """
    factories: list = []
    for ext in discover_extensions():
        if hasattr(ext, "background_tasks"):
            factories.extend(ext.background_tasks())
    return factories


def extension_config_descriptors() -> list:
    """Config-Control-Plane descriptors contributed by extensions (ADR-0150).

    The aggregate of every extension's ``config_descriptors()`` IS the Settings
    Registry (:class:`omnigent.config.ConfigRegistry`), served by the
    ``/v1/config`` REST surface. Empty when no extension contributes one.
    """
    descriptors: list = []
    for ext in discover_extensions():
        if hasattr(ext, "config_descriptors"):
            descriptors.extend(ext.config_descriptors())
    return descriptors


def extension_tool_interceptors() -> dict:
    """Tool-call interceptors contributed by extensions (``tool_interceptors()``).

    A ``{prefix: handler}`` table merged across extensions so core can claim an
    interception point (e.g. ``memory__*``) ahead of runner dispatch without a
    hard core→extension name reference (ADR-0143 §5 Step 1, BDP-2504). Mirrors
    the ``extension_secret_backends`` style — ``hasattr`` probe — but isolates a
    misbehaving contributor: one extension whose ``tool_interceptors`` raises is
    logged and skipped so the rest still register (it must never break boot).
    """
    interceptors: dict = {}
    for ext in discover_extensions():
        if hasattr(ext, "tool_interceptors"):
            try:
                interceptors.update(ext.tool_interceptors())
            except Exception:  # one bad extension must not break the others
                logger.exception(
                    "extension %r tool_interceptors() failed — skipped", ext.name
                )
    return interceptors
