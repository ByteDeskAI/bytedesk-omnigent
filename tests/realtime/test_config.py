"""BDP-2301 — config is read lazily from env (Infisical-bootstrapped) with a
realtime-specific override preferred over the canonical org key."""

from __future__ import annotations

from bytedesk_omnigent.realtime import config


def test_tenant_id_prefers_realtime_override(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_TENANT_ID", "specific")
    monkeypatch.setenv("BYTEDESK_TENANT_ID", "canonical")
    assert config.tenant_id() == "specific"


def test_tenant_id_falls_back_to_canonical(monkeypatch):
    monkeypatch.delenv("BYTEDESK_REALTIME_TENANT_ID", raising=False)
    monkeypatch.setenv("BYTEDESK_TENANT_ID", "canonical")
    assert config.tenant_id() == "canonical"


def test_tenant_id_none_when_unset(monkeypatch):
    monkeypatch.delenv("BYTEDESK_REALTIME_TENANT_ID", raising=False)
    monkeypatch.delenv("BYTEDESK_TENANT_ID", raising=False)
    assert config.tenant_id() is None


def test_redis_url_default_when_unset(monkeypatch):
    monkeypatch.delenv("BYTEDESK_REALTIME_REDIS_URL", raising=False)
    monkeypatch.delenv("BYTEDESK_REDIS_URL", raising=False)
    assert config.redis_url() == "redis://bytedesk-redis-master:6379"


def test_redis_url_override(monkeypatch):
    monkeypatch.setenv("BYTEDESK_REALTIME_REDIS_URL", "redis://custom:6379")
    assert config.redis_url() == "redis://custom:6379"
