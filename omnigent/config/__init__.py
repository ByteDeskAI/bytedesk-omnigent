"""Configuration Control Plane — the Settings Registry spine (ADR-0150, BDP-2413).

Every configurable omnigent property is a :class:`ConfigDescriptor` contributed
through the ``config_descriptors()`` extension surface (the same seam as
``tool_factories``/``policy_modules``), so config auto-discovers exactly like
tools and policies — no central key list to maintain, no platform involvement.
The aggregate of every extension's ``config_descriptors()`` IS the registry.

A descriptor's value lives in one of five substrates (process env, DB rows,
content-addressed bundle, in-memory registry, NATS object store). The
:class:`ConfigStore` Protocol + its five adapters (Strategy/Adapter, ADR-0008)
hide that heterogeneity so the REST surface never knows where a value lives.

This module is the **read path** (BDP-2413): descriptors, the store Protocol +
adapters' reads, and the registry that routes a read to the right adapter and
redacts secrets. The write port (BDP-2414) and write REST (BDP-2417) add the
mutating half on the same seam.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ── value + context types ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfigCtx:
    """Optional scoping for a read/write — which agent/session/policy row."""

    agent_id: str | None = None
    session_id: str | None = None
    policy_id: str | None = None


@dataclass(frozen=True)
class ConfigValue:
    """A resolved config value (or, for a secret, just its presence)."""

    key: str
    value: Any  # the value, or {"name", "present", "source"} for a secret
    etag: str | None
    source: str  # env | db_row | bundle | memory | nats
    writable: bool
    read_only_reason: str | None = None


# ── errors (HTTP-mapped in the write port, BDP-2414) ──────────────────────────


class ConfigError(Exception):
    """Base for config-plane errors."""


class ConfigReadOnlyError(ConfigError):
    """The descriptor is locked / deploy-only — never writable via REST (Tier 0)."""


class ConfigNotFoundError(ConfigError):
    """No descriptor registered for the key."""


class ConfigFloorError(ConfigError):
    """A Tier-1 value violates its safety floor — rejected at the port (HTTP 422)."""


class ConfigSchemaError(ConfigError):
    """The value does not match the descriptor's JSON-Schema (HTTP 422)."""


class ConfigConflictError(ConfigError):
    """If-Match precondition failed — a concurrent write moved the value (HTTP 412)."""


# ── descriptor ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfigDescriptor:
    """One configurable property — self-describing metadata + a read binding.

    The ``storage_source`` selects the :class:`ConfigStore` adapter; ``reader``
    is the storage-specific read built by that adapter. ``writer`` is ``None``
    in the read-only spine (BDP-2413) and is wired by the write port (BDP-2417);
    a ``None`` writer with a Tier-0 descriptor is permanently read-only.
    """

    key: str
    scope: str  # system|agent|session|policy|tool|content|extension
    what: str
    json_schema: dict[str, Any]  # self-describing validation contract
    tier: int  # 0 locked | 1 floor-guarded | 2 operator-editable | 3 content
    storage_source: str  # env | db_row | bundle | memory | nats
    reader: Callable[[ConfigCtx], Any]
    sensitivity: str = "public"  # public | secret (name+presence only)
    effect_timing: str = "live"  # live | requires_restart
    floor: str | None = None  # human description of the Tier-1 clamp
    change_event: str | None = None
    writer: Callable[..., Any] | None = None
    floor_check: Callable[[Any], Any] | None = None  # Tier-1: validate/clamp or raise
    etag_reader: Callable[[ConfigCtx], str | None] | None = None
    read_only_reason: str | None = None

    @property
    def writable(self) -> bool:
        """Editable via REST? Tier 0 (or no writer) is read-only."""
        return self.tier != 0 and self.writer is not None


# ── store Protocol + five adapters ────────────────────────────────────────────


@runtime_checkable
class ConfigStore(Protocol):
    """A storage substrate behind one or more descriptors (ADR-0150).

    ``read`` resolves a descriptor's current value; ``write`` mutates it (added
    by the write port, BDP-2417). One adapter per ``storage_source``.
    """

    source: str

    def read(self, descriptor: ConfigDescriptor, ctx: ConfigCtx) -> Any: ...


class _ReaderStore:
    """Base adapter: read is the descriptor's storage-specific ``reader``.

    The substrates differ at WRITE time (env is read-only, db is a guarded CAS,
    bundle is an ADR-0115 revision, …); for the read path they all resolve the
    descriptor's bound reader, so the concrete adapters below share this.
    """

    source = "?"

    def read(self, descriptor: ConfigDescriptor, ctx: ConfigCtx) -> Any:
        return descriptor.reader(ctx)


class EnvConfigStore(_ReaderStore):
    """Process env vars — deploy-only, always read-only (write raises)."""

    source = "env"

    @staticmethod
    def reader(env_var: str) -> Callable[[ConfigCtx], Any]:
        return lambda _ctx: os.environ.get(env_var)


class DbRowConfigStore(_ReaderStore):
    """DB rows (policies/conversations/cron/…): the only read+write substrate."""

    source = "db_row"


class BundleConfigStore(_ReaderStore):
    """Agent bundle config.yaml — read with expand_env=False (ADR-0115)."""

    source = "bundle"


class MemoryRegistryConfigStore(_ReaderStore):
    """In-memory pluggable registries / catalogs — read-only projection."""

    source = "memory"


class NatsObjectConfigStore(_ReaderStore):
    """NATS object store (artifact backend reachability/config) — read-only."""

    source = "nats"


_ADAPTERS: dict[str, ConfigStore] = {
    s.source: s()  # type: ignore[abstract]
    for s in (
        EnvConfigStore,
        DbRowConfigStore,
        BundleConfigStore,
        MemoryRegistryConfigStore,
        NatsObjectConfigStore,
    )
}


# ── registry (aggregate of every extension's config_descriptors()) ────────────


@dataclass
class ConfigRegistry:
    """The aggregate Settings Registry: descriptors keyed by ``key``.

    Built from :func:`omnigent.extensions.extension_config_descriptors`. Routes
    a read to the descriptor's storage adapter and redacts secrets (name +
    presence only, never the value).
    """

    _by_key: dict[str, ConfigDescriptor] = field(default_factory=dict)

    def register(self, descriptor: ConfigDescriptor) -> None:
        self._by_key[descriptor.key] = descriptor

    def descriptors(self) -> list[ConfigDescriptor]:
        return sorted(self._by_key.values(), key=lambda d: d.key)

    def get(self, key: str) -> ConfigDescriptor | None:
        return self._by_key.get(key)

    def read(self, key: str, ctx: ConfigCtx | None = None) -> ConfigValue:
        """Resolve a descriptor's current value (secrets → name+presence only)."""
        descriptor = self._by_key.get(key)
        if descriptor is None:
            raise ConfigNotFoundError(f"no config descriptor for {key!r}")
        ctx = ctx or ConfigCtx()
        adapter = _ADAPTERS[descriptor.storage_source]
        raw = adapter.read(descriptor, ctx)
        if descriptor.sensitivity == "secret":
            # Never expose the value — only name + presence + source.
            value: Any = {
                "name": descriptor.key,
                "present": raw is not None and raw != "",
                "source": descriptor.storage_source,
            }
        else:
            value = raw
        etag = descriptor.etag_reader(ctx) if descriptor.etag_reader else None
        return ConfigValue(
            key=key,
            value=value,
            etag=etag,
            source=descriptor.storage_source,
            writable=descriptor.writable,
            read_only_reason=descriptor.read_only_reason,
        )


def build_registry() -> ConfigRegistry:
    """Build the registry from every extension's ``config_descriptors()``."""
    from omnigent.extensions import extension_config_descriptors

    registry = ConfigRegistry()
    for descriptor in extension_config_descriptors():
        registry.register(descriptor)
    return registry


# ── write port (BDP-2414) ─────────────────────────────────────────────────────


def _json_type_ok(value: Any, json_type: str) -> bool:
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    return True  # unknown type → don't block


def _validate_schema(key: str, value: Any, schema: dict[str, Any]) -> None:
    """Minimal JSON-Schema check (type + enum) — no third-party dep (ADR-0150)."""
    json_type = schema.get("type")
    if json_type and not _json_type_ok(value, json_type):
        raise ConfigSchemaError(
            f"{key}: expected {json_type}, got {type(value).__name__}"
        )
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        raise ConfigSchemaError(f"{key}: {value!r} is not one of {enum}")


class RegistryConfigService:
    """The single write choke point — tier/floor/schema/If-Match enforced here.

    Every config write funnels through :meth:`write`: Tier-0 (or no writer) →
    ``ConfigReadOnlyError``; Tier-1 → the descriptor's ``floor_check`` (clamp or
    ``ConfigFloorError``); Tier-2/3 → JSON-Schema validation; then the
    descriptor's ``writer`` (the storage compare-and-swap, raising
    ``ConfigConflictError`` on a stale If-Match). The REST layer (BDP-2417) is a
    thin shell over this so a raw-API caller is governed identically to the UI.
    """

    def __init__(self, registry: ConfigRegistry) -> None:
        self._registry = registry

    def write(
        self,
        key: str,
        value: Any,
        *,
        if_match: str | None = None,
        ctx: ConfigCtx | None = None,
    ) -> ConfigValue:
        descriptor = self._registry.get(key)
        if descriptor is None:
            raise ConfigNotFoundError(f"no config descriptor for {key!r}")
        if not descriptor.writable:
            raise ConfigReadOnlyError(
                descriptor.read_only_reason
                or f"{key!r} is read-only (Tier {descriptor.tier})"
            )
        if descriptor.tier == 1 and descriptor.floor_check is not None:
            value = descriptor.floor_check(value)  # clamp, or raise ConfigFloorError
        _validate_schema(key, value, descriptor.json_schema)
        ctx = ctx or ConfigCtx()
        # The writer performs the storage write + If-Match CAS (raises
        # ConfigConflictError on a stale ETag).
        descriptor.writer(value, if_match, ctx)  # type: ignore[misc]
        return self._registry.read(key, ctx)


# ── runtime in-memory store for live-tunable descriptors (BDP-2417) ───────────


class RuntimeConfigStore:
    """A process-global, versioned in-memory store for live-tunable config.

    Each key carries a monotonic integer version as its ETag; a write is an
    If-Match compare-and-swap. Process-local: a multi-pod deploy propagates a
    change via the ``config.changed`` event (BDP-2418), not shared memory.
    """

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._versions: dict[str, int] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def version(self, key: str) -> int:
        return self._versions.get(key, 1)

    def set(self, key: str, value: Any, *, if_match: str | None = None) -> int:
        current = self._versions.get(key, 1)
        if if_match is not None and str(if_match) != str(current):
            raise ConfigConflictError(
                f"{key}: If-Match {if_match!r} is stale (current {current})"
            )
        self._values[key] = value
        self._versions[key] = current + 1
        return self._versions[key]


_RUNTIME_STORE = RuntimeConfigStore()


def runtime_store() -> RuntimeConfigStore:
    """The process-global runtime config store."""
    return _RUNTIME_STORE
