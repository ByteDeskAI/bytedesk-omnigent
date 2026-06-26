"""Central declaration of the live pluggable seams (BDP-2374).

This module is the single source of truth for *which* seams exist, *how* to reach
each one's :class:`~omnigent.pluggable.PluggableRegistry`, and *which* extension
hook contributes providers to it. Both the server-startup extension discovery and
the ``GET /v1/_capabilities`` manifest are projected from the one
:data:`SEAMS` table, so a new seam is wired in exactly one place.

**Server-only.** Importing this module imports the seam modules, some of which pull
the FastAPI stack (e.g. the server app, the spec parser). That is fine here: the
manifest is consumed only at server startup and from a server route — never on the
runner hot path. Per the just-fixed regression (BDP-2371), the seam modules
themselves must NOT call ``discover_extensions()`` at import; discovery is a
startup concern and lives in :func:`discover_all_extensions`, called once from the
server lifespan.

The registry accessors are intentionally *thunks* (zero-arg callables) rather than
pre-built instances so importing this manifest stays light. Each thunk returns a
stable per-process registry instance. That matters for the core plugin cutover:
first-party and third-party contributions must land on one durable seam plane,
not a throwaway registry created only for one manifest/discovery call. Registries
that need a location/URI use harmless placeholder values here because
``describe()`` and ``discover_extensions()`` only enumerate names / consult hooks;
they never invoke provider factories.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from omnigent.kernel.pluggable.registry import PluggableRegistry, _override_env_name

_logger = logging.getLogger(__name__)
_REGISTRY_CACHE: dict[str, PluggableRegistry[Any]] = {}


def _cached_registry(
    seam: str,
    build: Callable[[], PluggableRegistry[Any]],
) -> PluggableRegistry[Any]:
    """Return the stable registry instance for one manifest seam."""
    registry = _REGISTRY_CACHE.get(seam)
    if registry is None:
        registry = build()
        _REGISTRY_CACHE[seam] = registry
    return registry


def _harness_registry() -> PluggableRegistry[Any]:
    from omnigent.runtime.harnesses.descriptors import HARNESS_REGISTRY

    return HARNESS_REGISTRY


def _artifact_store_registry() -> PluggableRegistry[Any]:
    from omnigent.stores.factory import _build_artifact_store_registry

    return _cached_registry(
        "artifact_store",
        lambda: _build_artifact_store_registry("./artifacts"),
    )


def _web_search_registry() -> PluggableRegistry[Any]:
    from omnigent.tools.builtins.web_search import _build_provider_registry

    return _cached_registry("web_search", _build_provider_registry)


def _memory_embedder_registry() -> PluggableRegistry[Any]:
    from omnigent.stores.memory_store.provider import build_embedder_registry

    return _cached_registry("memory_embedder", build_embedder_registry)


def _agent_memory_registry() -> PluggableRegistry[Any]:
    from omnigent.stores.memory_store.provider import build_memory_provider_registry

    return _cached_registry(
        "agent_memory",
        lambda: build_memory_provider_registry("sqlite://"),
    )


def _spec_source_registry() -> PluggableRegistry[Any]:
    from omnigent.spec.source import spec_source_registry

    return spec_source_registry


def _coordination_backplane_registry() -> PluggableRegistry[Any]:
    from omnigent.coordination.factory import get_coordination_registry

    return get_coordination_registry()


def _assertion_verifier_registry() -> PluggableRegistry[Any]:
    from omnigent.identity.registry import build_assertion_verifier_registry

    return _cached_registry(
        "assertion_verifier",
        build_assertion_verifier_registry,
    )


def _outbound_credential_registry() -> PluggableRegistry[Any]:
    from omnigent.identity.registry import build_outbound_credential_registry

    return _cached_registry(
        "outbound_credential",
        build_outbound_credential_registry,
    )


def _authorizer_registry() -> PluggableRegistry[Any]:
    from omnigent.identity.registry import build_authorizer_registry

    return _cached_registry("authorizer", build_authorizer_registry)


# (seam_name, registry_accessor, extension_hook) — the one declaration that drives
# both startup discovery and the capability manifest. Adding a seam = one row here.
SEAMS: tuple[tuple[str, Callable[[], PluggableRegistry[Any]], str], ...] = (
    ("harness", _harness_registry, "harness_descriptors"),
    ("artifact_store", _artifact_store_registry, "artifact_store_providers"),
    ("web_search", _web_search_registry, "web_search_providers"),
    ("memory_embedder", _memory_embedder_registry, "memory_embedder_providers"),
    ("agent_memory", _agent_memory_registry, "agent_memory_providers"),
    ("spec_source", _spec_source_registry, "spec_source_providers"),
    (
        "coordination_backplane",
        _coordination_backplane_registry,
        "coordination_backplane_providers",
    ),
    ("assertion_verifier", _assertion_verifier_registry, "assertion_verifiers"),
    ("outbound_credential", _outbound_credential_registry, "outbound_credential_providers"),
    ("authorizer", _authorizer_registry, "authorization_providers"),
)


def discover_all_extensions() -> None:
    """Run extension discovery once across every live seam (server startup).

    Iterates :data:`SEAMS` and calls each registry's
    :meth:`~omnigent.pluggable.PluggableRegistry.discover_extensions` with that
    seam's hook. This is the ONLY place discovery is triggered — the seam modules
    deliberately do not discover at import (BDP-2371), so the FastAPI-heavy
    entry-point extensions stay off the runner hot path.

    Error-isolated: a seam whose registry can't even be built, or whose discovery
    raises, is logged and skipped so one bad seam/extension never breaks boot.
    Idempotent for the module-level singleton registries (re-running merely
    re-attempts registration; an already-registered provider is skipped by the
    registry's conflict guard, which is caught per-extension inside
    ``discover_extensions``). The per-call registries are freshly built each run.

    Note: the ``tools`` builtin registry is not a SEAMS row (its providers are
    config-accepting tool factories, not the zero-arg providers a standard seam
    holds), so its extension merge is driven separately at server startup via
    :func:`omnigent.tools.builtins.register_extension_tools`, alongside this call
    (see ``omnigent.server.app`` / ``omnigent.server.container``). Keeping it out
    of this loop preserves the one-discovery-per-seam contract.
    """
    for seam, accessor, hook in SEAMS:
        try:
            accessor().discover_extensions(hook=hook)
        except Exception:  # noqa: BLE001 — one bad seam must not break boot
            _logger.warning(
                "extension discovery failed for seam %r (hook %r)",
                seam,
                hook,
                exc_info=True,
            )


def capability_manifest() -> list[dict]:
    """The capability manifest: one JSON-serializable entry per live seam.

    Each entry is the registry's :meth:`~omnigent.pluggable.PluggableRegistry.describe`
    view (``seam``, registered ``names``, ``active`` impl, ``default``) plus the
    ``override_env`` var (``OMNIGENT_USE_<SEAM>``) that pins the active impl per
    environment. A seam whose registry can't be built is reported with an ``error``
    entry rather than omitted, so the manifest is always complete.

    :returns: A list of seam dicts, ordered as declared in :data:`SEAMS`.
    """
    manifest: list[dict] = []
    for seam, accessor, _hook in SEAMS:
        try:
            described = accessor().describe()
            described["override_env"] = _override_env_name(seam)
            manifest.append(described)
        except Exception as exc:  # noqa: BLE001 — keep the manifest complete
            _logger.warning("capability_manifest: seam %r unavailable", seam, exc_info=True)
            manifest.append(
                {
                    "seam": seam,
                    "names": [],
                    "active": None,
                    "default": None,
                    "override_env": _override_env_name(seam),
                    "error": type(exc).__name__,
                }
            )
    return manifest


__all__ = ["SEAMS", "capability_manifest", "discover_all_extensions"]
