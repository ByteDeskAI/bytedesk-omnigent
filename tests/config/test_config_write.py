"""RegistryConfigService write port — tier/floor/schema/If-Match enforcement (BDP-2414)."""

from __future__ import annotations

import pytest

from omnigent.config import (
    ConfigConflictError,
    ConfigFloorError,
    ConfigNotFoundError,
    ConfigReadOnlyError,
    ConfigSchemaError,
    RegistryConfigService,
    build_registry,
)

_MODEL = "system.default_ad_hoc_model"
_CEILING = "policies.cost_hard_stop.default_ceiling_usd"


def _svc() -> RegistryConfigService:
    return RegistryConfigService(build_registry())


def test_write_tier0_is_read_only() -> None:
    with pytest.raises(ConfigReadOnlyError):
        _svc().write("system.nats.url", "nats://x", if_match=None)


def test_write_unknown_key_not_found() -> None:
    with pytest.raises(ConfigNotFoundError):
        _svc().write("nope.nope", "x", if_match=None)


def test_write_tier2_succeeds_and_bumps_etag() -> None:
    reg = build_registry()
    svc = RegistryConfigService(reg)
    cur = reg.read(_MODEL)
    new = svc.write(_MODEL, "claude-opus-4-8", if_match=cur.etag)
    assert new.value == "claude-opus-4-8"
    assert new.etag != cur.etag  # the version (ETag) advanced


def test_write_tier2_stale_if_match_conflicts() -> None:
    reg = build_registry()
    svc = RegistryConfigService(reg)
    cur = reg.read(_MODEL)
    svc.write(_MODEL, "m1", if_match=cur.etag)  # advances the version
    with pytest.raises(ConfigConflictError):
        svc.write(_MODEL, "m2", if_match=cur.etag)  # stale ETag


def test_write_tier2_schema_rejects_wrong_type() -> None:
    with pytest.raises(ConfigSchemaError):
        _svc().write(_MODEL, 123, if_match=None)  # descriptor expects a string


def test_write_tier1_floor_rejects_nonpositive() -> None:
    with pytest.raises(ConfigFloorError):
        _svc().write(_CEILING, 0, if_match=None)


def test_write_tier1_floor_accepts_valid() -> None:
    reg = build_registry()
    svc = RegistryConfigService(reg)
    cur = reg.read(_CEILING)
    new = svc.write(_CEILING, 25.0, if_match=cur.etag)
    assert new.value == 25.0


def test_write_emits_metadata_only_change() -> None:
    """A successful write publishes a config.changed carrying NO value (BDP-2418)."""
    reg = build_registry()
    seen: list[object] = []
    svc = RegistryConfigService(reg, on_change=seen.append)
    cur = reg.read(_MODEL)
    new = svc.write(_MODEL, "claude-opus-4-8", if_match=cur.etag)

    assert len(seen) == 1
    change = seen[0]
    assert change.key == _MODEL
    assert change.scope == "system"
    assert change.etag == new.etag
    assert change.tier == 2
    assert change.effect_timing in ("live", "requires_restart")
    # The change is metadata only — it must not carry the value.
    assert not hasattr(change, "value")


def test_failed_write_emits_no_change() -> None:
    """A rejected write (stale ETag) does not emit a change."""
    reg = build_registry()
    seen: list[object] = []
    svc = RegistryConfigService(reg, on_change=seen.append)
    cur = reg.read(_MODEL)
    svc.write(_MODEL, "m1", if_match=cur.etag)  # 1 emit
    with pytest.raises(ConfigConflictError):
        svc.write(_MODEL, "m2", if_match=cur.etag)  # stale → raises, no emit
    assert len(seen) == 1
