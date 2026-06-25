"""Tests for omnigent.kernel.service_registry (BDP-2327, Phase 1)."""

from __future__ import annotations

import pytest

from omnigent.kernel.service_registry import ServiceRegistry


class _A:
    """A distinct service type."""


class _B:
    """Another distinct service type."""


def test_register_returns_the_instance() -> None:
    """register() returns its argument so it can wrap an inline build."""
    registry = ServiceRegistry()
    a = _A()
    assert registry.register(a) is a


def test_get_retrieves_by_type() -> None:
    """A registered service is retrievable under its own type."""
    registry = ServiceRegistry()
    a = _A()
    b = _B()
    registry.register(a)
    registry.register(b)
    assert registry.get(_A) is a
    assert registry.get(_B) is b


def test_get_missing_raises_keyerror() -> None:
    """get() on an unregistered type raises KeyError with the type name."""
    registry = ServiceRegistry()
    with pytest.raises(KeyError, match="_A"):
        registry.get(_A)


def test_try_get_missing_returns_none() -> None:
    """try_get() is the optional variant: None when absent, instance when present."""
    registry = ServiceRegistry()
    assert registry.try_get(_A) is None
    a = _A()
    registry.register(a)
    assert registry.try_get(_A) is a


def test_register_as_type_keys_under_explicit_type() -> None:
    """as_type registers under a supplied key (e.g. a Protocol/base)."""
    registry = ServiceRegistry()

    class _Impl(_A):
        pass

    impl = _Impl()
    registry.register(impl, as_type=_A)
    assert registry.get(_A) is impl
    assert _A in registry
    assert _Impl not in registry


def test_re_register_replaces() -> None:
    """Registering a second instance of a type replaces the first."""
    registry = ServiceRegistry()
    first = _A()
    second = _A()
    registry.register(first)
    registry.register(second)
    assert registry.get(_A) is second


def test_contains() -> None:
    """__contains__ reports membership by type."""
    registry = ServiceRegistry()
    assert _A not in registry
    registry.register(_A())
    assert _A in registry
