"""Tests for the agent ``capabilities:`` spec field (BDP-2334).

``capabilities`` is a first-class, immutable sequence of capability
slugs on :class:`omnigent.spec.types.AgentSpec`. It declares the
capability surface a sibling resolver consumes; this module pins the
parse + validation contract:

- the field parses from the top-level YAML ``capabilities:`` list,
- defaults to an empty immutable tuple when omitted,
- validates as a list of non-empty strings,
- and rejects every malformed shape (non-list, non-string entry,
  empty / blank entry, duplicate slug).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.spec.validator import validate


def _write_config(root: Path, config: dict[str, object]) -> Path:
    """Write *config* as ``config.yaml`` under *root* and return *root*."""
    (root / "config.yaml").write_text(yaml.dump(config))
    return root


# ── Parsing ──────────────────────────────────────────────────────


def test_parse_capabilities_absent_defaults_to_empty_tuple(tmp_path: Path) -> None:
    """An omitted ``capabilities:`` block yields an empty immutable tuple."""
    _write_config(tmp_path, {"spec_version": 1, "name": "no-caps"})
    spec = parse(tmp_path)
    assert spec.capabilities == ()
    assert isinstance(spec.capabilities, tuple)


def test_parse_capabilities_list(tmp_path: Path) -> None:
    """A ``capabilities:`` list parses into a tuple of slugs in order."""
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "with-caps",
            "capabilities": ["office.read", "office.write", "email.send"],
        },
    )
    spec = parse(tmp_path)
    assert spec.capabilities == ("office.read", "office.write", "email.send")
    assert isinstance(spec.capabilities, tuple)


def test_parse_capabilities_empty_list(tmp_path: Path) -> None:
    """An explicit empty list parses into the empty tuple."""
    _write_config(tmp_path, {"spec_version": 1, "name": "empty-caps", "capabilities": []})
    spec = parse(tmp_path)
    assert spec.capabilities == ()


def test_parse_capabilities_not_a_list(tmp_path: Path) -> None:
    """A non-list ``capabilities:`` value is rejected at parse time."""
    _write_config(
        tmp_path,
        {"spec_version": 1, "name": "bad-caps", "capabilities": "office.read"},
    )
    with pytest.raises(OmnigentError, match=r"capabilities"):
        parse(tmp_path)


def test_parse_capabilities_non_string_entry(tmp_path: Path) -> None:
    """A non-string entry in ``capabilities:`` is rejected at parse time."""
    _write_config(
        tmp_path,
        {"spec_version": 1, "name": "bad-caps", "capabilities": ["office.read", 7]},
    )
    with pytest.raises(OmnigentError, match=r"capabilities"):
        parse(tmp_path)


def test_parse_capabilities_blank_entry(tmp_path: Path) -> None:
    """A blank / whitespace-only entry is rejected at parse time."""
    _write_config(
        tmp_path,
        {"spec_version": 1, "name": "bad-caps", "capabilities": ["office.read", "  "]},
    )
    with pytest.raises(OmnigentError, match=r"capabilities"):
        parse(tmp_path)


# ── Validation ───────────────────────────────────────────────────


def _spec_with_capabilities(capabilities: object) -> AgentSpec:
    """Build a minimal spec carrying *capabilities* for validator tests.

    Bypasses the parser so programmatically-constructed specs (tests,
    translators, future API callers) hit the same validation gate.
    """
    return AgentSpec(
        spec_version=1,
        name="caps-agent",
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
        capabilities=capabilities,  # type: ignore[arg-type]
    )


def test_validate_capabilities_valid() -> None:
    """A list of non-empty slugs passes validation."""
    result = validate(_spec_with_capabilities(("office.read", "office.write")))
    assert result.valid


def test_validate_capabilities_empty_valid() -> None:
    """The default empty tuple passes validation."""
    result = validate(_spec_with_capabilities(()))
    assert result.valid


def test_validate_capabilities_non_string_entry() -> None:
    """A non-string entry fails validation with a ``capabilities`` path."""
    result = validate(_spec_with_capabilities(("office.read", 7)))
    assert not result.valid
    assert any("capabilities" in e.path for e in result.errors)


def test_validate_capabilities_empty_entry() -> None:
    """An empty-string slug fails validation with a ``capabilities`` path."""
    result = validate(_spec_with_capabilities(("office.read", "")))
    assert not result.valid
    assert any("capabilities" in e.path for e in result.errors)


def test_validate_capabilities_blank_entry() -> None:
    """A whitespace-only slug fails validation with a ``capabilities`` path."""
    result = validate(_spec_with_capabilities(("office.read", "   ")))
    assert not result.valid
    assert any("capabilities" in e.path for e in result.errors)


def test_validate_capabilities_duplicate() -> None:
    """A duplicate slug fails validation with a ``capabilities`` path."""
    result = validate(_spec_with_capabilities(("office.read", "office.read")))
    assert not result.valid
    assert any("capabilities" in e.path for e in result.errors)
