"""Tests for omnigent.pluggable.PluggableRegistry (BDP-2345).

The registry is the generic seam scaffold every pluggability ticket builds on:
register/get/default + an OMNIGENT_USE_<SEAM> override + entry-point discovery
that error-isolates a bad extension. Tests inject fakes so no installed
entry-point metadata is required.
"""

from __future__ import annotations

import pytest

from omnigent.pluggable import (
    PluggableRegistry,
    ProviderNotRegistered,
    RegistryConflict,
)


def test_register_and_get_returns_factory_result() -> None:
    reg: PluggableRegistry[str] = PluggableRegistry("widget")
    reg.register("a", lambda: "value-a")
    assert reg.get("a") == "value-a"


def test_default_registered_at_construction() -> None:
    reg: PluggableRegistry[str] = PluggableRegistry(
        "widget", default=("local", lambda: "the-default")
    )
    assert "local" in reg.names()
    assert reg.resolve_default() == "the-default"


def test_resolve_default_without_default_raises() -> None:
    reg: PluggableRegistry[str] = PluggableRegistry("widget")
    with pytest.raises(ProviderNotRegistered) as exc:
        reg.resolve_default()
    assert "widget" in str(exc.value)


def test_duplicate_register_raises_registry_conflict() -> None:
    reg: PluggableRegistry[str] = PluggableRegistry("widget")
    reg.register("a", lambda: "1")
    with pytest.raises(RegistryConflict) as exc:
        reg.register("a", lambda: "2")
    assert "a" in str(exc.value)
    assert "widget" in str(exc.value)


def test_get_unknown_raises_listing_known_names() -> None:
    reg: PluggableRegistry[str] = PluggableRegistry("widget")
    reg.register("a", lambda: "1")
    reg.register("b", lambda: "2")
    with pytest.raises(ProviderNotRegistered) as exc:
        reg.get("zzz")
    msg = str(exc.value)
    assert "zzz" in msg
    assert "a" in msg and "b" in msg


def test_override_env_selects_active_impl(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_USE_WIDGET", "remote")
    reg: PluggableRegistry[str] = PluggableRegistry(
        "widget", default=("local", lambda: "local-v")
    )
    reg.register("remote", lambda: "remote-v")
    assert reg.resolve_default() == "remote-v"


def test_override_env_unknown_name_raises(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_USE_WIDGET", "nope")
    reg: PluggableRegistry[str] = PluggableRegistry(
        "widget", default=("local", lambda: "local-v")
    )
    with pytest.raises(ProviderNotRegistered) as exc:
        reg.resolve_default()
    assert "nope" in str(exc.value)


def test_override_env_empty_uses_default(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_USE_WIDGET", "  ")
    reg: PluggableRegistry[str] = PluggableRegistry(
        "widget", default=("local", lambda: "local-v")
    )
    assert reg.resolve_default() == "local-v"


def test_active_name_reflects_override(monkeypatch) -> None:
    reg: PluggableRegistry[str] = PluggableRegistry(
        "widget", default=("local", lambda: "local-v")
    )
    reg.register("remote", lambda: "remote-v")
    assert reg.describe()["active"] == "local"
    monkeypatch.setenv("OMNIGENT_USE_WIDGET", "remote")
    assert reg.describe()["active"] == "remote"


def test_describe_shape() -> None:
    reg: PluggableRegistry[str] = PluggableRegistry(
        "widget", default=("local", lambda: "local-v")
    )
    reg.register("remote", lambda: "remote-v")
    d = reg.describe()
    assert d["seam"] == "widget"
    assert sorted(d["names"]) == ["local", "remote"]
    assert d["default"] == "local"
    assert d["active"] == "local"


def test_names_lists_registered() -> None:
    reg: PluggableRegistry[str] = PluggableRegistry("widget")
    reg.register("a", lambda: "1")
    reg.register("b", lambda: "2")
    assert sorted(reg.names()) == ["a", "b"]


# ── entry-point discovery ────────────────────────────────────────────────────


class _GoodExt:
    name = "good"

    def widget_providers(self) -> dict:
        return {"contributed": lambda: "from-ext"}


class _BadExt:
    name = "bad"

    def widget_providers(self) -> dict:
        raise RuntimeError("boom")


def test_discover_extensions_registers_contributed(monkeypatch) -> None:
    import omnigent.kernel.pluggable.registry as regmod

    monkeypatch.setattr(regmod, "discover_extensions", lambda: [_GoodExt()])
    reg: PluggableRegistry[str] = PluggableRegistry("widget")
    reg.discover_extensions(hook="widget_providers")
    assert reg.get("contributed") == "from-ext"


def test_discover_extensions_isolates_bad_extension(monkeypatch) -> None:
    import omnigent.kernel.pluggable.registry as regmod

    # Bad ext first: it must not prevent the good ext from registering.
    monkeypatch.setattr(
        regmod, "discover_extensions", lambda: [_BadExt(), _GoodExt()]
    )
    reg: PluggableRegistry[str] = PluggableRegistry("widget")
    reg.discover_extensions(hook="widget_providers")
    assert reg.get("contributed") == "from-ext"
    assert "bad" not in reg.names() and "contributed" in reg.names()


def test_discover_extensions_skips_ext_without_hook(monkeypatch) -> None:
    import omnigent.kernel.pluggable.registry as regmod

    class _NoHook:
        name = "nohook"

    monkeypatch.setattr(regmod, "discover_extensions", lambda: [_NoHook()])
    reg: PluggableRegistry[str] = PluggableRegistry("widget")
    reg.discover_extensions(hook="widget_providers")  # no raise
    assert reg.names() == []
