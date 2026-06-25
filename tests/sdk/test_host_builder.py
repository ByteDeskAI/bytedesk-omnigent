"""SDK ``Host`` fluent-builder tests (BDP-2508, Section 12.4).

The builder is a named-argument collector that produces the SAME ``create_app``
call a hand-written composition root would, and feeds explicit extensions /
disables through the kernel's existing discovery seam — not a parallel list. We
verify the wiring without booting the full FastAPI stack by patching
``create_app`` and the kernel discovery symbol.
"""

from __future__ import annotations

import os

import pytest

import omnigent.extensions as kext
from omnigent.sdk import Host, extension, tool


class _FakeTool:
    pass


def test_build_returns_host():
    assert isinstance(Host.build(), Host)


def test_with_store_and_options_collected(monkeypatch):
    captured = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return "APP"

    monkeypatch.setattr("omnigent.server.app.create_app", fake_create_app)

    sentinel_store = object()
    app = (
        Host.build()
        .with_store(conversation_store=sentinel_store)
        .with_option(admins=["a@b.c"])
        .build_app()
    )
    assert app == "APP"
    assert captured["conversation_store"] is sentinel_store
    assert captured["admins"] == ["a@b.c"]


def test_unknown_param_rejected():
    with pytest.raises(TypeError):
        Host.build().with_store(not_a_real_param=1)


def test_with_auth_sets_provider(monkeypatch):
    captured = {}
    monkeypatch.setattr("omnigent.server.app.create_app", lambda **kw: captured.update(kw))
    ap, ps = object(), object()
    Host.build().with_auth(auth_provider=ap, permission_store=ps).build_app()
    assert captured["auth_provider"] is ap
    assert captured["permission_store"] is ps


def test_with_extension_dedupes_by_name():
    @extension(name="dup-ext")
    class Ext:
        @tool(name="t")
        def t(self):
            return _FakeTool()

    h = Host.build().with_extension(Ext()).with_extension(Ext())
    assert len(h._extensions) == 1


def test_explicit_extension_prepended_to_discovery(monkeypatch):
    @extension(name="explicit-ext")
    class Ext:
        @tool(name="t")
        def t(self):
            return _FakeTool()

    discovered_inside = {}

    def fake_create_app(**kwargs):
        # Inside the build, the kernel discovery sees the explicit extension.
        discovered_inside["names"] = [e.name for e in kext.discover_extensions()]
        return "APP"

    monkeypatch.setattr("omnigent.server.app.create_app", fake_create_app)
    # Baseline discovery (no explicit) — restore-after is asserted below.
    monkeypatch.setattr(kext, "discover_extensions", lambda: [])

    ext = Ext()
    Host.build().with_extension(ext).build_app()
    assert "explicit-ext" in discovered_inside["names"]
    # After build, the kernel symbol is restored to the original.
    assert kext.discover_extensions() == []


def test_disable_sets_and_restores_env(monkeypatch):
    monkeypatch.setattr("omnigent.server.app.create_app", lambda **kw: "APP")
    monkeypatch.delenv(kext.DISABLED_ENV_VAR, raising=False)

    seen = {}

    def fake_create_app(**kwargs):
        seen["disabled"] = os.environ.get(kext.DISABLED_ENV_VAR)
        return "APP"

    monkeypatch.setattr("omnigent.server.app.create_app", fake_create_app)
    Host.build().disable("omnigent.realtime").build_app()
    assert "omnigent.realtime" in (seen["disabled"] or "")
    # Restored (was unset) after build.
    assert kext.DISABLED_ENV_VAR not in os.environ
