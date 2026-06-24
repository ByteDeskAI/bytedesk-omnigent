"""Edge-case coverage for :mod:`omnigent.policies.registry`."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import omnigent.policies.registry as registry


@pytest.fixture(autouse=True)
def _reload_registry_after_each_test() -> None:
    yield
    registry.load_registry()


def test_get_entry_returns_none_for_unknown_handler() -> None:
    registry.load_registry()
    assert registry.get_entry("totally.unknown.handler") is None


def test_is_registered_handler_lazy_loads_registry_when_empty() -> None:
    registry._registry.clear()
    registry._registry_by_handler.clear()
    assert registry.is_registered_handler(
        "omnigent.policies.builtins.safety.ask_on_os_tools",
    )


def test_load_registry_skips_unimportable_extra_module() -> None:
    registry.load_registry(
        extra_modules=["definitely.not.a.real.policy.module_xyz"],
    )
    assert registry.get_registry()


def test_load_registry_skips_module_without_policy_registry_list() -> None:
    mod_name = "_registry_test_no_list"
    sys.modules[mod_name] = types.ModuleType(mod_name)
    try:
        registry.load_registry(extra_modules=[mod_name])
        assert all(e.handler != f"{mod_name}.anything" for e in registry.get_registry())
    finally:
        sys.modules.pop(mod_name, None)


def test_load_registry_skips_malformed_registry_entries() -> None:
    mod_name = "_registry_test_malformed"
    mod = types.ModuleType(mod_name)
    mod.POLICY_REGISTRY = [
        {"name": "missing handler key"},
        {
            "handler": f"{mod_name}.good_eval",
            "kind": "callable",
            "description": "ok",
        },
    ]
    sys.modules[mod_name] = mod
    try:
        registry.load_registry(extra_modules=[mod_name])
        assert registry.get_entry(f"{mod_name}.good_eval") is not None
    finally:
        sys.modules.pop(mod_name, None)


def test_validate_factory_params_accepts_any_params_when_schema_missing() -> None:
    mod_name = "_registry_test_no_schema_factory"
    handler = f"{mod_name}.factory"
    mod = types.ModuleType(mod_name)
    mod.POLICY_REGISTRY = [
        {
            "handler": handler,
            "kind": "factory",
            "description": "schema-less factory",
        },
    ]
    sys.modules[mod_name] = mod
    try:
        registry.load_registry(extra_modules=[mod_name])
        assert registry.validate_factory_params(handler, {"anything": 1}) is None
    finally:
        sys.modules.pop(mod_name, None)


def test_validate_factory_params_none_reports_missing_required_without_default() -> None:
    mod_name = "_registry_test_strict_factory"
    handler = f"{mod_name}.factory"
    mod = types.ModuleType(mod_name)
    mod.POLICY_REGISTRY = [
        {
            "handler": handler,
            "kind": "factory",
            "description": "strict",
            "params_schema": {
                "type": "object",
                "required": ["limit"],
                "properties": {"limit": {"type": "integer"}},
            },
        },
    ]
    sys.modules[mod_name] = mod
    try:
        registry.load_registry(extra_modules=[mod_name])
        error = registry.validate_factory_params(handler, None)
        assert error is not None
        assert "requires params" in error
        assert "limit" in error
    finally:
        sys.modules.pop(mod_name, None)


def test_validate_factory_params_none_passes_when_required_have_defaults() -> None:
    mod_name = "_registry_test_defaulted_required"
    handler = f"{mod_name}.factory"
    mod = types.ModuleType(mod_name)
    mod.POLICY_REGISTRY = [
        {
            "handler": handler,
            "kind": "factory",
            "description": "defaulted required",
            "params_schema": {
                "type": "object",
                "required": ["limit"],
                "properties": {
                    "limit": {"type": "integer", "default": 5},
                },
            },
        },
    ]
    sys.modules[mod_name] = mod
    try:
        registry.load_registry(extra_modules=[mod_name])
        assert registry.validate_factory_params(handler, None) is None
    finally:
        sys.modules.pop(mod_name, None)


def test_validate_factory_params_skips_type_check_when_property_schema_is_null() -> None:
    mod_name = "_registry_test_null_property"
    handler = f"{mod_name}.factory"
    mod = types.ModuleType(mod_name)
    mod.POLICY_REGISTRY = [
        {
            "handler": handler,
            "kind": "factory",
            "description": "null property schema",
            "params_schema": {
                "type": "object",
                "properties": {"limit": None},
            },
        },
    ]
    sys.modules[mod_name] = mod
    try:
        registry.load_registry(extra_modules=[mod_name])
        assert registry.validate_factory_params(handler, {"limit": "anything"}) is None
    finally:
        sys.modules.pop(mod_name, None)