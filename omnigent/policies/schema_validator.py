"""Schema validator — pluggable type-checker for policy factory params (BDP-2361, P10).

The policy registry validates a factory's ``factory_params`` against its declared
JSON-Schema ``params_schema``. The per-value type check was a hand-rolled
``_type_matches(value, json_type)`` covering the six JSON-Schema scalar/container
types. That is fine for today's simple schemas but can't grow (no ``enum``,
``format``, nested ``object`` properties, ``oneOf`` …).

This module wraps that check in a :class:`SchemaValidator` Adapter so a fuller
JSON-Schema implementation (e.g. the ``jsonschema`` package) can be slotted in
later without touching the registry. The default
(:class:`BuiltinTypeMatchValidator`) reproduces the historical ``_type_matches``
behavior byte-for-byte. Selection goes through the ``schema_validator`` pluggable
seam.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from omnigent.pluggable import PluggableRegistry


@runtime_checkable
class SchemaValidator(Protocol):
    """Check whether a Python value conforms to a JSON-Schema type."""

    def type_matches(self, value: Any, json_type: str) -> bool:
        """Return ``True`` if *value* matches JSON-Schema *json_type*.

        :param value: The value to check.
        :param json_type: JSON-Schema type string (``"integer"``, ``"string"``,
            ``"object"`` …). An unrecognized type is accepted (``True``) — the
            registry only rejects on a *known* type mismatch.
        """
        ...


class BuiltinTypeMatchValidator:
    """The historical ``_type_matches`` policy-param checker, unchanged.

    Reproduces the prior behavior exactly: ``bool`` is *not* an integer/number
    (Python's ``bool`` is an ``int`` subclass, so the explicit guard matters),
    and an unknown JSON type is accepted rather than rejected.
    """

    def type_matches(self, value: Any, json_type: str) -> bool:
        if json_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if json_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if json_type == "string":
            return isinstance(value, str)
        if json_type == "boolean":
            return isinstance(value, bool)
        if json_type == "array":
            return isinstance(value, list)
        if json_type == "object":
            return isinstance(value, dict)
        # Unknown type — don't reject.
        return True


def schema_validator_registry() -> PluggableRegistry[SchemaValidator]:
    """Build the ``schema_validator`` seam registry with the built-in default.

    The default reproduces the legacy ``_type_matches``. A fuller JSON-Schema
    validator can be registered (and selected via ``OMNIGENT_USE_SCHEMA_VALIDATOR``
    or an extension ``schema_validator_providers`` hook) without changing the
    policy registry.

    :returns: A registry whose default is :class:`BuiltinTypeMatchValidator`.
    """
    return PluggableRegistry(
        "schema_validator", default=("builtin_type_match", BuiltinTypeMatchValidator)
    )


def default_schema_validator() -> SchemaValidator:
    """Resolve the active schema validator (default = builtin type-match)."""
    return schema_validator_registry().resolve_default()


__all__ = [
    "BuiltinTypeMatchValidator",
    "SchemaValidator",
    "default_schema_validator",
    "schema_validator_registry",
]
