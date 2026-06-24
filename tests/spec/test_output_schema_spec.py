"""Tests for the structured-output seam (BDP-2393, ADR-0149).

Two halves:
- the agent ``output_schema:`` spec field parses from top-level YAML into
  :attr:`AgentSpec.output_schema` (``None`` when absent / non-dict), and
- :func:`omnigent.spec.output_schema.validate_output` validates a free-text
  reply against that schema (Strategy validator + Message Translator),
  tolerant about extracting the JSON but strict about validating it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnigent.spec.output_schema import extract_json, validate_output
from omnigent.spec.parser import parse


def _write_config(root: Path, config: dict[str, object]) -> Path:
    (root / "config.yaml").write_text(yaml.dump(config))
    return root


# ── Parsing ──────────────────────────────────────────────────────


def test_parse_output_schema_absent_defaults_to_none(tmp_path: Path) -> None:
    _write_config(tmp_path, {"spec_version": 1, "name": "free-text"})
    assert parse(tmp_path).output_schema is None


def test_parse_output_schema_dict(tmp_path: Path) -> None:
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    _write_config(tmp_path, {"spec_version": 1, "name": "structured", "output_schema": schema})
    assert parse(tmp_path).output_schema == schema


def test_parse_output_schema_non_dict_is_ignored(tmp_path: Path) -> None:
    """A non-mapping output_schema is treated as absent (lenient default)."""
    _write_config(
        tmp_path, {"spec_version": 1, "name": "bad", "output_schema": "not-a-schema"}
    )
    assert parse(tmp_path).output_schema is None


# ── Validation ───────────────────────────────────────────────────

_SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def test_validate_plain_json() -> None:
    r = validate_output(_SCHEMA, '{"ok": true}')
    assert r.valid and r.value == {"ok": True} and not r.errors


def test_validate_fenced_json() -> None:
    r = validate_output(_SCHEMA, 'sure:\n```json\n{"ok": false}\n```\nthanks')
    assert r.valid and r.value == {"ok": False}


def test_validate_schema_mismatch_returns_value_and_errors() -> None:
    r = validate_output(_SCHEMA, '{"ok": "nope"}')
    assert not r.valid
    assert r.value == {"ok": "nope"}  # surfaced so the caller sees what came back
    assert r.errors


def test_validate_no_json() -> None:
    r = validate_output(_SCHEMA, "there is no json in this reply")
    assert not r.valid and r.value is None and r.errors


def test_validate_invalid_schema_definition() -> None:
    """A malformed schema definition surfaces as an output_schema error."""
    bad_schema = {"type": 123}
    r = validate_output(bad_schema, '{"ok": true}')
    assert not r.valid
    assert r.value == {"ok": True}
    assert r.errors and r.errors[0].startswith("invalid output_schema:")


def test_extract_json_prefers_fence_then_whole_then_span() -> None:
    assert extract_json('```json\n{"a":1}\n```')[1] == {"a": 1}
    assert extract_json('{"a":1}')[1] == {"a": 1}
    assert extract_json('prefix {"a":1} suffix')[1] == {"a": 1}
    assert extract_json("nothing") == (False, None)
