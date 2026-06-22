"""Structured-output validation (BDP-2393, ADR-0149).

The structured-output seam: an agent declares ``output_schema`` (a JSON
Schema) and a consumer validates the agent's final reply against it
(GoF Strategy validator + EIP Message Translator / Canonical Data Model).

This module is the validator Strategy. It is deliberately tolerant about
*extracting* the JSON object from a free-text reply — models wrap JSON in
``` ```json ``` fences or surround it with prose — but strict about
*validating* the extracted object against the schema.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import jsonschema

# Matches a ```json … ``` (or bare ``` … ```) fenced block; group 1 is the body.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class StructuredOutputResult:
    """Outcome of validating a reply against an ``output_schema``.

    :param valid: ``True`` iff a JSON object was extracted AND it conforms
        to the schema.
    :param value: The parsed JSON value when extraction succeeded, else
        ``None``. Present even when ``valid`` is ``False`` due to a schema
        mismatch, so callers can surface what the model actually produced.
    :param errors: Human-readable validation/extraction errors; empty when
        ``valid``.
    """

    valid: bool
    value: Any = None
    errors: tuple[str, ...] = field(default_factory=tuple)


def extract_json(text: str) -> tuple[bool, Any]:
    """
    Best-effort extraction of a JSON value from a free-text reply.

    Tries, in order: a fenced ```json``` block, the whole string, then the
    first ``{...}``/``[...]`` span. Returns ``(found, value)``.

    :param text: The agent's reply text.
    :returns: ``(True, value)`` on the first successful parse, else
        ``(False, None)``.
    """
    candidates: list[str] = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text.strip())
    # First balanced-looking object/array span as a last resort.
    span = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if span:
        candidates.append(span.group(1))
    for candidate in candidates:
        try:
            return True, json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return False, None


def validate_output(schema: dict[str, Any], text: str) -> StructuredOutputResult:
    """
    Validate an agent reply against a JSON ``output_schema``.

    :param schema: The JSON Schema (the agent spec's ``output_schema``).
    :param text: The agent's final reply text.
    :returns: A :class:`StructuredOutputResult`.
    """
    found, value = extract_json(text)
    if not found:
        return StructuredOutputResult(False, None, ("reply did not contain parseable JSON",))
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as exc:
        return StructuredOutputResult(False, value, (exc.message,))
    except jsonschema.SchemaError as exc:
        return StructuredOutputResult(False, value, (f"invalid output_schema: {exc.message}",))
    return StructuredOutputResult(True, value, ())


if __name__ == "__main__":  # pragma: no cover - runnable self-check
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    assert validate_output(schema, '{"ok": true}').valid
    assert validate_output(schema, 'here you go:\n```json\n{"ok": false}\n```').valid
    assert not validate_output(schema, '{"ok": "nope"}').valid
    assert not validate_output(schema, "no json here").valid
    print("output_schema self-check OK")
