"""Unit tests for :mod:`omnigent.tools._runner` subprocess entry point."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import omnigent.tools._runner as runner


def _stdin_mock(payload: bytes) -> MagicMock:
    """Return a stdin stand-in whose ``buffer.read()`` returns ``payload``."""
    mock_stdin = MagicMock()
    mock_stdin.buffer = MagicMock(read=MagicMock(return_value=payload))
    return mock_stdin


def test_main_invalid_json_writes_error(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(runner.sys, "stdin", _stdin_mock(b"not-json"))
    monkeypatch.setattr(runner, "_write_error", lambda msg: written.append({"error": msg}))
    runner.main()
    assert written and "Invalid request JSON" in written[0]["error"]


def test_main_missing_tool_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner.sys,
        "stdin",
        _stdin_mock(json.dumps({"module_path": str(tmp_path / "x.py")}).encode()),
    )
    monkeypatch.setattr(runner, "_write_error", lambda msg: written.append({"error": msg}))
    runner.main()
    assert "tool_name" in written[0]["error"]


def test_load_module_empty_path() -> None:
    written: list[str] = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner, "_write_error", lambda msg: written.append(msg))
    assert runner._load_module("") is None
    assert "Empty module_path" in written[0]
    monkeypatch.undo()


def test_resolve_tool_function_branches(tmp_path: Path) -> None:
    module = ModuleType("fake")
    module.bad = "not-callable"
    written: list[str] = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner, "_write_error", lambda msg: written.append(msg))

    assert runner._resolve_tool_function(module, "missing") is None
    assert "not found" in written[-1]

    assert runner._resolve_tool_function(module, "bad") is None
    assert "not callable" in written[-1]

    def plain() -> str:
        return "ok"

    module.plain = plain
    assert runner._resolve_tool_function(module, "plain") is None
    assert "@tool" in written[-1]
    monkeypatch.undo()


def test_maybe_inject_tool_state_requires_root() -> None:
    def uses_state(*, tool_state: Any) -> str:
        return "ok"

    with pytest.raises(RuntimeError, match="no state_root was provided by the parent"):
        runner._maybe_inject_tool_state(uses_state, {}, None)


def test_maybe_inject_tool_state_injects_when_declared(tmp_path: Path) -> None:
    def uses_state(*, tool_state: Any) -> str:
        return "ok"

    args: dict[str, Any] = {}
    runner._maybe_inject_tool_state(uses_state, args, str(tmp_path))
    assert "tool_state" in args


def test_invoke_tool_runs_async_coroutine() -> None:
    async def async_tool() -> str:
        return "async-ok"

    assert runner._invoke_tool(async_tool, {}) == "async-ok"


def test_serialize_result_string_passthrough() -> None:
    def fn() -> str:
        return "plain"

    assert runner._serialize_result(fn, "hello") == "hello"


def test_serialize_result_json_fallback() -> None:
    def fn() -> dict[str, int]:
        return {"n": 1}

    assert json.loads(runner._serialize_result(fn, {"n": 1})) == {"n": 1}


def test_serialize_result_unserializable() -> None:
    class _Weird:
        def __str__(self) -> str:
            raise TypeError("cannot stringify")

    def fn() -> _Weird:
        return _Weird()

    out = runner._serialize_result(fn, _Weird())
    assert out.startswith("<unserializable return value")


def test_write_response_stdout_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_AP_RESPONSE_MODE", "stdout")
    buf = MagicMock()
    mock_stdout = MagicMock()
    mock_stdout.fileno.return_value = runner.sys.stdout.fileno()
    mock_stdout.buffer = buf
    monkeypatch.setattr(runner.sys, "stdout", mock_stdout)
    runner._write_response({"result": "ok"})
    assert buf.write.called
    assert buf.flush.called


def test_get_output_fd_defaults_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("_AP_RESPONSE_MODE", raising=False)
    monkeypatch.setenv("_AP_RESPONSE_FD", "7")
    assert runner._get_output_fd() == 7


def test_main_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool_file = tmp_path / "echo.py"
    tool_file.write_text(
        textwrap.dedent(
            '''\
            from omnigent_client import tool


            @tool
            def echo(value: str) -> str:
                """Echo."""
                return f"echo:{value}"
            '''
        )
    )
    request = {
        "module_path": str(tool_file),
        "tool_name": "echo",
        "arguments": {"value": "hi"},
    }
    monkeypatch.setattr(runner.sys, "stdin", _stdin_mock(json.dumps(request).encode()))

    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("_AP_RESPONSE_FD", str(write_fd))
    monkeypatch.delenv("_AP_RESPONSE_MODE", raising=False)

    runner.main()
    raw = os.read(read_fd, 4096)
    os.close(read_fd)
    payload = json.loads(raw)
    assert payload["result"] == "echo:hi"


def test_main_module_load_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    written: list[dict[str, Any]] = []
    request = {
        "module_path": str(tmp_path / "missing.py"),
        "tool_name": "any",
        "arguments": {},
    }
    monkeypatch.setattr(runner.sys, "stdin", _stdin_mock(json.dumps(request).encode()))
    monkeypatch.setattr(runner, "_write_error", lambda msg: written.append({"error": msg}))
    runner.main()
    assert written and "Import error" in written[0]["error"]


def test_main_tool_resolution_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    written: list[dict[str, Any]] = []
    tool_file = tmp_path / "empty.py"
    tool_file.write_text("")
    request = {
        "module_path": str(tool_file),
        "tool_name": "missing_fn",
        "arguments": {},
    }
    monkeypatch.setattr(runner.sys, "stdin", _stdin_mock(json.dumps(request).encode()))
    monkeypatch.setattr(runner, "_write_error", lambda msg: written.append({"error": msg}))
    runner.main()
    assert written and "not found" in written[0]["error"]


def test_main_invoke_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    written: list[dict[str, Any]] = []
    tool_file = tmp_path / "boom.py"
    tool_file.write_text(
        textwrap.dedent(
            '''\
            from omnigent_client import tool


            @tool
            def boom() -> str:
                """Boom."""
                raise RuntimeError("kaboom")
            '''
        )
    )
    request = {
        "module_path": str(tool_file),
        "tool_name": "boom",
        "arguments": {},
    }
    monkeypatch.setattr(runner.sys, "stdin", _stdin_mock(json.dumps(request).encode()))
    monkeypatch.setattr(runner, "_write_error", lambda msg: written.append({"error": msg}))
    runner.main()
    assert written and "RuntimeError" in written[0]["error"]


def test_load_module_bad_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []
    monkeypatch.setattr(runner, "_write_error", lambda msg: written.append(msg))
    monkeypatch.setattr(
        runner.importlib.util,
        "spec_from_file_location",
        lambda *_a, **_k: None,
    )
    assert runner._load_module("/some/path.py") is None
    assert "Cannot create module spec" in written[0]


def test_maybe_inject_tool_state_non_introspectable(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    args: dict[str, Any] = {"x": 1}

    def uses_state(*, tool_state: Any) -> str:
        return "ok"

    monkeypatch.setattr(
        inspect,
        "signature",
        MagicMock(side_effect=TypeError("no signature")),
    )
    runner._maybe_inject_tool_state(uses_state, args, "/tmp")
    assert args == {"x": 1}


def _tool_with_return_annotation(return_annotation: Any) -> Any:
    def typed() -> Any:
        return None

    setattr(
        typed,
        runner._TOOL_MARKER_ATTR,
        SimpleNamespace(return_annotation=return_annotation),
    )
    return typed


def test_serialize_result_type_adapter_path() -> None:
    fn = _tool_with_return_annotation(dict[str, int])
    assert json.loads(runner._serialize_result(fn, {"n": 42})) == {"n": 42}


def test_serialize_result_type_adapter_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenAdapter:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def dump_json(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise RuntimeError("adapter failed")

    monkeypatch.setattr("pydantic.TypeAdapter", _BrokenAdapter)
    fn = _tool_with_return_annotation(dict[str, int])
    out = runner._serialize_result(fn, {"not": "int"})
    assert json.loads(out) == {"not": "int"}


def test_write_response_fd_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("_AP_RESPONSE_FD", str(write_fd))
    monkeypatch.delenv("_AP_RESPONSE_MODE", raising=False)
    runner._write_response({"result": "fd-ok"})
    raw = os.read(read_fd, 4096)
    os.close(read_fd)
    assert json.loads(raw) == {"result": "fd-ok"}


def test_write_error_delegates_to_write_response(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(runner, "_write_response", captured.append)
    runner._write_error("oops")
    assert captured == [{"error": "oops"}]


def test_main_entrypoint_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", _stdin_mock(b"not-json"))
    monkeypatch.setenv("_AP_RESPONSE_FD", str(write_fd))
    monkeypatch.delenv("_AP_RESPONSE_MODE", raising=False)
    runpy.run_path(runner.__file__, run_name="__main__")
    raw = os.read(read_fd, 4096)
    os.close(read_fd)
    payload = json.loads(raw)
    assert "Invalid request JSON" in payload["error"]