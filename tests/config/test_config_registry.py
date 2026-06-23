"""Settings Registry spine — aggregation, read routing, secret redaction (BDP-2413)."""

from __future__ import annotations

import pytest

from omnigent.config import ConfigNotFoundError, build_registry


def test_build_registry_aggregates_extension_descriptors() -> None:
    reg = build_registry()
    keys = {d.key for d in reg.descriptors()}
    assert {"system.log_level", "system.nats.url", "system.database.uri"} <= keys


def test_read_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_LOG_LEVEL", "DEBUG")
    v = build_registry().read("system.log_level")
    assert v.value == "DEBUG"
    assert v.source == "env"


def test_secret_is_redacted_to_name_and_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATABASE_URI", "postgres://user:s3cr3t@host/db")
    v = build_registry().read("system.database.uri")
    # NEVER the value — name + presence + source only.
    assert v.value == {"name": "system.database.uri", "present": True, "source": "env"}
    assert "s3cr3t" not in str(v.value)


def test_secret_absent_reports_present_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_INFISICAL_CLIENT_SECRET", raising=False)
    v = build_registry().read("system.infisical.client_secret")
    assert v.value["present"] is False


def test_tier0_and_unwired_writer_are_not_writable() -> None:
    reg = build_registry()
    assert reg.get("system.nats.url").writable is False  # Tier 0 locked
    # Tier-2 descriptor still not writable in the read spine (no writer yet).
    assert reg.get("system.log_level").writable is False
    assert reg.get("system.nats.url").read_only_reason is not None


def test_read_unknown_key_raises() -> None:
    with pytest.raises(ConfigNotFoundError):
        build_registry().read("does.not.exist")


def test_memory_descriptor_reads_loaded_extensions() -> None:
    v = build_registry().read("system.extensions.loaded")
    assert "bytedesk" in v.value  # the bytedesk extension is discovered at boot
