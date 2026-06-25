"""SDK — the ``Host`` fluent builder (BDP-2508, Section 12.4).

A named-argument collector that produces the **same** ``create_app()`` call a
hand-written composition root would. It is *not* a second source of truth and it
does **not** introduce a parallel discovery/lifecycle/registry: explicit
extensions added via :meth:`Host.with_extension` are fed to the kernel's
existing ``discover_extensions`` / ``install_extensions`` path (by prepending
them to the discovery results for the duration of the build), and
:meth:`Host.disable` maps onto the kernel's ``OMNIGENT_DISABLED_EXTENSIONS``
env-var filter. The 15-parameter ``create_app`` signature is hidden behind a
fluent chain::

    from omnigent.sdk import Host

    app = (
        Host.build()
        .with_store(conversation_store=store, artifact_store=art, ...)
        .with_extension(MyExtension())
        .disable("omnigent.realtime")
        .build_app()
    )

``build_app()`` imports ``create_app`` lazily (it drags in the full FastAPI /
domain stack), so importing :mod:`omnigent.sdk` stays kernel-light.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

#: The ``create_app`` store parameters callers populate via ``with_store`` /
#: ``with_auth`` / ``with_option``. Kept as a frozen allow-list so a typo'd
#: builder kwarg fails loudly instead of being silently dropped into create_app.
_CREATE_APP_PARAMS: frozenset[str] = frozenset(
    {
        "agent_store",
        "file_store",
        "conversation_store",
        "artifact_store",
        "agent_cache",
        "runner_tunnel_tokens",
        "comment_store",
        "policy_store",
        "permission_store",
        "auth_provider",
        "host_store",
        "account_store",
        "extra_routers",
        "policy_modules",
        "admins",
        "allowed_domains",
        "sandbox_config",
    }
)


class Host:
    """Fluent composition-root builder over ``omnigent.server.app.create_app``."""

    def __init__(self) -> None:
        self._params: dict[str, Any] = {}
        self._extensions: list[Any] = []
        self._disabled: set[str] = set()

    # ── entry point ───────────────────────────────────────────────────
    @staticmethod
    def build() -> "Host":
        """Start a fluent composition root (Builder pattern)."""
        return Host()

    # ── fluent setters ────────────────────────────────────────────────
    def with_store(self, **stores: Any) -> "Host":
        """Set one or more store / collaborator ``create_app`` arguments."""
        self._set(stores)
        return self

    def with_auth(self, *, auth_provider: Any = None, permission_store: Any = None) -> "Host":
        """Set the auth provider and (optionally) the permission store."""
        params: dict[str, Any] = {}
        if auth_provider is not None:
            params["auth_provider"] = auth_provider
        if permission_store is not None:
            params["permission_store"] = permission_store
        self._set(params)
        return self

    def with_option(self, **options: Any) -> "Host":
        """Set any other ``create_app`` keyword argument (admins, policy_modules, …)."""
        self._set(options)
        return self

    def with_extension(self, ext: Any) -> "Host":
        """Add an explicit extension instance to install (deduped by ``name``)."""
        name = getattr(ext, "name", None)
        if name is not None and any(getattr(e, "name", None) == name for e in self._extensions):
            return self
        self._extensions.append(ext)
        return self

    def disable(self, *names: str) -> "Host":
        """Disable extensions by name (the ``OMNIGENT_DISABLED_EXTENSIONS`` analog)."""
        self._disabled.update(n for n in names if n)
        return self

    # ── terminal ──────────────────────────────────────────────────────
    def build_app(self) -> Any:
        """Compile the builder down to a single ``create_app()`` call → FastAPI."""
        from omnigent.server.app import create_app

        with self._discovery_context():
            return create_app(**self._params)

    # ── internals ─────────────────────────────────────────────────────
    def _set(self, params: dict[str, Any]) -> None:
        unknown = set(params) - _CREATE_APP_PARAMS
        if unknown:
            raise TypeError(
                f"unknown create_app parameter(s): {sorted(unknown)}; "
                f"valid: {sorted(_CREATE_APP_PARAMS)}"
            )
        self._params.update(params)

    @contextlib.contextmanager
    def _discovery_context(self) -> Iterator[None]:
        """Feed explicit extensions + disables through the kernel's discovery seam.

        Prepends ``self._extensions`` to ``discover_extensions()``'s result (so
        ``install_extensions`` and every ``PluggableRegistry.discover_extensions``
        sees them) and unions ``self._disabled`` into
        ``OMNIGENT_DISABLED_EXTENSIONS`` — using the kernel's own mechanisms, not
        a parallel list. Restored on exit.
        """
        import omnigent.extensions as kext

        prev_disabled = os.environ.get(kext.DISABLED_ENV_VAR)
        if self._disabled:
            merged = set(filter(None, (prev_disabled or "").split(","))) | self._disabled
            os.environ[kext.DISABLED_ENV_VAR] = ",".join(sorted(merged))

        original = kext.discover_extensions
        explicit = list(self._extensions)

        def _discover_with_explicit():
            discovered = original()
            seen = {getattr(e, "name", None) for e in explicit}
            out = list(explicit)
            for ext in discovered:
                if getattr(ext, "name", None) not in seen:
                    out.append(ext)
            return out

        if explicit:
            kext.discover_extensions = _discover_with_explicit  # type: ignore[assignment]
        try:
            yield
        finally:
            kext.discover_extensions = original  # type: ignore[assignment]
            if self._disabled:
                if prev_disabled is None:
                    os.environ.pop(kext.DISABLED_ENV_VAR, None)
                else:
                    os.environ[kext.DISABLED_ENV_VAR] = prev_disabled


__all__ = ["Host"]
