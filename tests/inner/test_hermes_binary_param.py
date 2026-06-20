"""Seam tests for the Hermes binary constructor param (BDP-2349 #42).

Proves: the binary is a constructor param (no import-time module global), an
explicit param wins, and the HARNESS_HERMES_BIN env remains the default — and
the chosen binary is what ``start()`` spawns.
"""
from __future__ import annotations

import pytest

from bytedesk_omnigent.harnesses.hermes_native_executor import (
    HermesNativeExecutor,
    _HermesAcpSession,
    _default_hermes_bin,
)


def test_default_is_env_then_literal(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_HERMES_BIN", raising=False)
    assert _default_hermes_bin() == "hermes"
    monkeypatch.setenv("HARNESS_HERMES_BIN", "/opt/hermes/bin/hermes")
    assert _default_hermes_bin() == "/opt/hermes/bin/hermes"


def test_explicit_param_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_HERMES_BIN", "/from/env/hermes")
    ex = HermesNativeExecutor(hermes_bin="/explicit/hermes")
    assert ex._hermes_bin == "/explicit/hermes"
    assert ex._session._hermes_bin == "/explicit/hermes"


def test_falls_back_to_env_default(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_HERMES_BIN", "/from/env/hermes")
    ex = HermesNativeExecutor()
    assert ex._hermes_bin == "/from/env/hermes"


@pytest.mark.asyncio
async def test_start_spawns_the_param_binary(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    session = _HermesAcpSession(cwd="/tmp", model=None, hermes_bin="/my/hermes")

    async def fake_start_process(argv, cwd):
        captured["argv"] = argv

    async def fake_request(method, params, timeout):
        return {"result": {"sessionId": "sess-1"}}

    monkeypatch.setattr(session, "_start_process", fake_start_process)
    monkeypatch.setattr(session, "_request", fake_request)

    await session.start()
    assert captured["argv"] == ["/my/hermes", "acp"]
