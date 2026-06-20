"""Schema-validator Adapter tests (BDP-2361, P10).

Proves the default ``BuiltinTypeMatchValidator`` matches the historical
``_type_matches`` on representative inputs (including the ``bool``-is-not-int
edge and the unknown-type accept), the registry resolves it as the default,
the policy registry's ``_type_matches`` wrapper delegates to it, and the seam
is swappable.
"""

from __future__ import annotations

import pytest

from omnigent.pluggable import PluggableRegistry
from omnigent.policies.registry import _type_matches
from omnigent.policies.schema_validator import (
    BuiltinTypeMatchValidator,
    SchemaValidator,
    default_schema_validator,
    schema_validator_registry,
)

# (value, json_type, expected) — covers every branch + edges.
_CASES = [
    (5, "integer", True),
    (True, "integer", False),  # bool is not an integer
    (5, "number", True),
    (5.0, "number", True),
    (True, "number", False),  # bool is not a number
    ("x", "string", True),
    (5, "string", False),
    (True, "boolean", True),
    (1, "boolean", False),
    ([1, 2], "array", True),
    ("x", "array", False),
    ({"a": 1}, "object", True),
    ([1], "object", False),
    (5, "null", True),  # unknown type — accepted
    ("anything", "unknown-future-type", True),  # unknown type — accepted
]


@pytest.mark.parametrize(("value", "json_type", "expected"), _CASES)
def test_builtin_validator_matches_legacy(value: object, json_type: str, expected: bool) -> None:
    assert BuiltinTypeMatchValidator().type_matches(value, json_type) is expected


@pytest.mark.parametrize(("value", "json_type", "expected"), _CASES)
def test_registry_wrapper_delegates(value: object, json_type: str, expected: bool) -> None:
    # The policy registry's _type_matches must yield identical results.
    assert _type_matches(value, json_type) is expected


def test_default_resolves_to_builtin() -> None:
    assert isinstance(default_schema_validator(), BuiltinTypeMatchValidator)
    assert schema_validator_registry().names() == ["builtin_type_match"]


def test_validator_satisfies_protocol() -> None:
    assert isinstance(BuiltinTypeMatchValidator(), SchemaValidator)


def test_seam_swappable_with_fake() -> None:
    class AlwaysFalse:
        def type_matches(self, value: object, json_type: str) -> bool:
            return False

    registry: PluggableRegistry[SchemaValidator] = PluggableRegistry(
        "schema_validator", default=("fake", AlwaysFalse)
    )
    assert registry.resolve_default().type_matches(5, "integer") is False
