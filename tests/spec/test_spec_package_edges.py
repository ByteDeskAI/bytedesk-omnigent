"""Edge-case coverage for omnigent.spec package internals."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec import load
import omnigent.spec as spec_pkg
from omnigent.spec._omnigent_compat import (
    diagnose_yaml_rejection,
    is_omnigent_yaml,
    load_omnigent_yaml,
)
from omnigent.spec._omnigent_legacy_shim import (
    _coerce_legacy_return,
    _convert_args,
    _has_legacy_signature,
    _maybe_parse_json,
    _positional_arity,
    _v0_event_to_legacy_content,
    _wrap_legacy,
)
from omnigent.spec.validator import ValidationResult


def _omnigent_bundle_dir(tmp_path: Path, *, name: str = "root") -> Path:
    """Minimal native bundle directory with harness executor."""
    (tmp_path / "config.yaml").write_text(
        yaml.dump(
            {
                "spec_version": 1,
                "name": name,
                "executor": {
                    "type": "omnigent",
                    "config": {"harness": "claude-sdk"},
                },
            }
        )
    )
    return tmp_path


# ── __init__: _find_omnigent_yaml_in_dir ─────────────────────


def test_find_omnigent_yaml_in_dir_returns_single_match(tmp_path: Path) -> None:
    """Exactly one omnigent YAML at bundle root resolves to that file."""
    (tmp_path / "agent.yaml").write_text("name: solo\nprompt: hi\n")
    found = spec_pkg._find_omnigent_yaml_in_dir(tmp_path)
    assert found == tmp_path / "agent.yaml"


def test_find_omnigent_yaml_in_dir_returns_none_with_config_yaml(tmp_path: Path) -> None:
    """config.yaml present disqualifies single-file omnigent dispatch."""
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: x\n")
    (tmp_path / "extra.yaml").write_text("name: extra\nprompt: hi\n")
    assert spec_pkg._find_omnigent_yaml_in_dir(tmp_path) is None


def test_find_omnigent_yaml_in_dir_returns_none_with_multiple_yaml_files(
    tmp_path: Path,
) -> None:
    """Ambiguous roots (zero or many omnigent YAMLs) return None."""
    (tmp_path / "one.yaml").write_text("name: one\nprompt: a\n")
    (tmp_path / "two.yaml").write_text("name: two\nprompt: b\n")
    assert spec_pkg._find_omnigent_yaml_in_dir(tmp_path) is None


# ── __init__: enforce_handler_allowlist on config.yaml path ──


def test_load_config_yaml_rejects_unregistered_handler_when_enforced(
    tmp_path: Path,
) -> None:
    """Post-parse scan blocks unregistered function-policy handlers."""
    bundle = _omnigent_bundle_dir(tmp_path)
    config = yaml.safe_load((bundle / "config.yaml").read_text())
    config["guardrails"] = {
        "policies": {
            "rce": {
                "type": "function",
                "on": ["request"],
                "handler": "subprocess.Popen",
            }
        }
    }
    (bundle / "config.yaml").write_text(yaml.dump(config))

    with pytest.raises(OmnigentError, match=r"not a registered policy handler"):
        load(bundle, enforce_handler_allowlist=True)


def test_load_config_yaml_rejects_unregistered_handler_in_sub_agent(
    tmp_path: Path,
) -> None:
    """Handler allowlist recursion reaches nested agents/ configs."""
    bundle = _omnigent_bundle_dir(tmp_path, name="parent")
    parent = yaml.safe_load((bundle / "config.yaml").read_text())
    parent["tools"] = {"agents": ["evil"]}
    (bundle / "config.yaml").write_text(yaml.dump(parent))

    child_dir = bundle / "agents" / "evil"
    child_dir.mkdir(parents=True)
    child_config = {
        "spec_version": 1,
        "name": "evil",
        "executor": {
            "type": "omnigent",
            "config": {"harness": "claude-sdk"},
        },
        "prompt": "hi",
        "guardrails": {
            "policies": {
                "rce": {
                    "type": "function",
                    "on": ["request"],
                    "function": "subprocess.Popen",
                }
            }
        },
    }
    (child_dir / "config.yaml").write_text(yaml.dump(child_config))

    with pytest.raises(OmnigentError, match=r"not a registered policy handler"):
        load(bundle, enforce_handler_allowlist=True)


# ── _omnigent_compat: detection + diagnosis ─────────────────


def test_is_omnigent_yaml_rejects_non_mapping_root(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- item\n")
    assert is_omnigent_yaml(path) is False


def test_is_omnigent_yaml_rejects_missing_name(tmp_path: Path) -> None:
    path = tmp_path / "no_name.yaml"
    path.write_text("prompt: hi\n")
    assert is_omnigent_yaml(path) is False


def test_diagnose_yaml_rejection_reports_wrong_extension(tmp_path: Path) -> None:
    path = tmp_path / "agent.txt"
    path.write_text("name: x\nprompt: hi\n")
    assert "expected '.yaml' or '.yml'" in diagnose_yaml_rejection(path)


def test_diagnose_yaml_rejection_reports_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert "empty" in diagnose_yaml_rejection(path)


def test_diagnose_yaml_rejection_reports_non_mapping_root(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- item\n")
    msg = diagnose_yaml_rejection(path)
    assert "mapping" in msg
    assert "list" in msg


def test_diagnose_yaml_rejection_reports_missing_name(tmp_path: Path) -> None:
    path = tmp_path / "no_name.yaml"
    path.write_text("prompt: hi\n")
    assert "missing required key 'name'" in diagnose_yaml_rejection(path)


def test_diagnose_yaml_rejection_unknown_reason_guard(tmp_path: Path) -> None:
    """Valid omnigent YAML still yields the internal fallback string."""
    path = tmp_path / "valid.yaml"
    path.write_text("name: ok\nprompt: hi\n")
    assert "unknown reason" in diagnose_yaml_rejection(path)


# ── _omnigent_compat: load_omnigent_yaml branches ───────────


def test_load_omnigent_yaml_coerces_non_mapping_raw_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dict raw YAML roots are tolerated as an empty mapping."""
    path = tmp_path / "hello.yaml"
    path.write_text(
        yaml.dump(
            {
                "name": "hello",
                "prompt": "hi",
                "executor": {"harness": "claude-sdk"},
            }
        )
    )

    import yaml as pyyaml

    from omnigent.inner.loader import _OmnigentYamlLoader

    real_load = pyyaml.load
    load_calls = 0

    def _load_second_returns_list(*args: Any, **kwargs: Any) -> Any:
        nonlocal load_calls
        load_calls += 1
        if load_calls >= 2 and kwargs.get("Loader") is _OmnigentYamlLoader:
            return ["not", "a", "dict"]
        return real_load(*args, **kwargs)

    monkeypatch.setattr(pyyaml, "load", _load_second_returns_list)

    spec = load_omnigent_yaml(path)
    assert spec.name == "hello"


def test_load_omnigent_yaml_raises_when_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "hello.yaml"
    path.write_text("name: hello\nprompt: hi\nexecutor:\n  harness: claude-sdk\n")

    def _invalid(_spec: Any) -> ValidationResult:
        result = ValidationResult()
        result.add("name", "synthetic failure")
        return result

    monkeypatch.setattr("omnigent.spec.validator.validate", _invalid)

    with pytest.raises(OmnigentError, match=r"invalid agent spec synthesized"):
        load_omnigent_yaml(path)


# ── _omnigent_legacy_shim: remaining branches ────────────────


def test_has_legacy_signature_returns_false_when_introspection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signature failures are treated as modern callables."""

    def _legacy(content: Any, phase: str) -> dict[str, Any]:
        del content, phase
        return {"action": "allow"}

    def _raise_type_error(_fn: Any) -> Any:
        raise TypeError("no signature")

    monkeypatch.setattr("omnigent.spec._omnigent_legacy_shim.inspect.signature", _raise_type_error)
    assert _has_legacy_signature(_legacy) is False


def test_coerce_legacy_return_passes_through_non_action_dicts() -> None:
    payload = {"decision": {"result": "ALLOW"}}
    assert _coerce_legacy_return(payload) is payload


@pytest.mark.asyncio
async def test_wrap_legacy_async_callable_converts_legacy_return() -> None:
    async def _legacy_async(content: Any, phase: str) -> dict[str, Any]:
        del content
        if phase == "tool_call":
            return {"action": "deny", "reason": "blocked"}
        return {"action": "allow"}

    wrapped = _wrap_legacy(_legacy_async)
    event = {
        "type": "tool_call",
        "data": {"tool": "sleep", "args": {"seconds": 1}},
        "target": "sleep",
    }
    result = await wrapped(event, {})
    assert result == {"result": "DENY", "reason": "blocked"}


def test_v0_event_to_legacy_content_parses_tool_result_json() -> None:
    payload = {"status": "ok"}
    event = {"type": "tool_result", "data": json.dumps(payload)}
    assert _v0_event_to_legacy_content(event) == payload


def test_convert_args_event_dict_three_arg_includes_legacy_context() -> None:
    event = {
        "type": "tool_call",
        "data": {"tool": "grep", "args": {"pattern": "x"}},
        "target": "grep",
        "context": {"labels": {"env": "prod"}},
    }
    args = _convert_args(
        event,
        {"ignored": True},
        wants_context=True,
        configured_phases=["tool_call"],
    )
    assert args[0] == {"tool": "grep", "args": {"pattern": "x"}}
    assert args[1] == "tool_call"
    assert args[2] == {
        "labels": {"env": "prod"},
        "configured_phases": ["tool_call"],
        "tool_name": "grep",
    }


def test_maybe_parse_json_passes_non_strings_through() -> None:
    value = {"already": "parsed"}
    assert _maybe_parse_json(value) is value


def test_positional_arity_defaults_to_two_when_introspection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _legacy(content: Any, phase: str, context: dict[str, Any]) -> dict[str, Any]:
        del content, phase, context
        return {"action": "allow"}

    def _raise_type_error(_fn: Any) -> Any:
        raise TypeError("no signature")

    monkeypatch.setattr("omnigent.spec._omnigent_legacy_shim.inspect.signature", _raise_type_error)
    assert _positional_arity(_legacy) == 2


@pytest.mark.asyncio
async def test_wrap_legacy_async_forwards_reset_turn_attribute() -> None:
    reset_calls: list[None] = []

    async def _legacy_async(content: Any, phase: str) -> dict[str, Any]:
        del content, phase
        return {"action": "allow"}

    def _reset() -> None:
        reset_calls.append(None)

    _legacy_async.reset_turn = _reset  # type: ignore[attr-defined]

    wrapped = _wrap_legacy(_legacy_async)
    assert hasattr(wrapped, "reset_turn")
    wrapped.reset_turn()
    assert len(reset_calls) == 1


def test_wrap_legacy_async_via_event_dict_sync_entry() -> None:
    """Async shim is awaitable from asyncio.run for inner-system events."""

    async def _legacy(content: Any, phase: str) -> dict[str, Any]:
        del content, phase
        return {"action": "allow"}

    wrapped = _wrap_legacy(_legacy)
    event = {"type": "request", "data": "hello"}
    result = asyncio.run(wrapped(event, {}))
    assert result == {"result": "ALLOW"}