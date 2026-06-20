"""Typing-hygiene tests for the ByteDesk policy registry shape (BDP-2358).

The 8 ``bytedesk_omnigent.policies.*`` modules annotate their ``POLICY_REGISTRY``
lists with the shared :class:`~bytedesk_omnigent.policies.PolicyRegistryRaw`
TypedDict, whose ``kind`` is the closed
:data:`~omnigent.policies.registry.PolicyHandlerKind` Literal. These tests pin
the wire contract at runtime so a malformed entry (missing ``handler`` / bad
``kind``) is caught rather than silently skipped.
"""

from __future__ import annotations

import importlib
import typing

from bytedesk_omnigent.policies import PolicyHandlerKind, PolicyRegistryRaw

# The 8 first-party policy modules that export a POLICY_REGISTRY.
_BYTEDESK_POLICY_MODULES = [
    "bytedesk_omnigent.policies.budget",
    "bytedesk_omnigent.policies.delegation",
    "bytedesk_omnigent.policies.dry_run",
    "bytedesk_omnigent.policies.forever_gate",
    "bytedesk_omnigent.policies.outreach_compliance",
    "bytedesk_omnigent.policies.spawn_governor",
    "bytedesk_omnigent.policies.two_key",
    "bytedesk_omnigent.policies.verify_gate",
]


def test_policy_handler_kind_is_closed_two_value_literal() -> None:
    """PolicyHandlerKind is exactly ``Literal["callable", "factory"]`` — the two
    branches the registry's ``validate_factory_params`` is total over."""
    assert set(typing.get_args(PolicyHandlerKind)) == {"callable", "factory"}


def test_policy_registry_raw_required_and_optional_keys() -> None:
    """The TypedDict requires ``handler`` + ``kind`` and leaves the display
    fields optional, mirroring what ``load_registry`` reads."""
    assert PolicyRegistryRaw.__required_keys__ == frozenset({"handler", "kind"})
    assert PolicyRegistryRaw.__optional_keys__ == frozenset(
        {"name", "description", "params_schema"}
    )


def test_all_bytedesk_policy_entries_conform_to_raw_shape() -> None:
    """Every real entry in all 8 modules has a string ``handler`` and a ``kind``
    inside the closed set — the value-level guarantee the TypedDict encodes."""
    allowed_kinds = set(typing.get_args(PolicyHandlerKind))
    seen = 0
    for module_path in _BYTEDESK_POLICY_MODULES:
        mod = importlib.import_module(module_path)
        registry = mod.POLICY_REGISTRY
        assert isinstance(registry, list) and registry, module_path
        for entry in registry:
            seen += 1
            assert isinstance(entry["handler"], str) and entry["handler"], entry
            assert entry["kind"] in allowed_kinds, entry
    assert seen >= len(_BYTEDESK_POLICY_MODULES)


def test_malformed_entry_missing_handler_is_rejected_by_loader() -> None:
    """A ``POLICY_REGISTRY`` entry without ``handler`` (the kind of mistake the
    TypedDict catches statically) is skipped by the core loader, never crashing
    discovery — so the static guarantee has a runtime safety net too."""
    import sys
    import types

    from omnigent.policies.registry import is_registered_handler, load_registry

    fake = types.ModuleType("fake_bad_policy_module")
    # Missing the required "handler" key.
    fake.POLICY_REGISTRY = [{"kind": "callable", "name": "broken"}]  # type: ignore[attr-defined]
    sys.modules["fake_bad_policy_module"] = fake
    try:
        load_registry(extra_modules=["fake_bad_policy_module"])
        # The malformed entry contributed nothing to the registry.
        assert not is_registered_handler("broken")
    finally:
        del sys.modules["fake_bad_policy_module"]
        load_registry()
