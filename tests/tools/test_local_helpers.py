"""Unit tests for :mod:`omnigent.tools.local` helpers and edge paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omnigent.spec.types import LocalToolInfo
from omnigent.tools.base import ToolContext
from omnigent.tools.local import (
    LocalPythonTool,
    LocalToolLoadError,
    _import_tool_module,
    _read_fd3_response,
    _read_stdout_response,
    _scan_inline_metadata,
    _STDOUT_RESPONSE_PREFIX,
    _write_srt_settings_file,
    load_local_python_tools,
)


def _make_tool(tmp_path: Path) -> LocalPythonTool:
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "demo.py").write_text(
        '"""Demo."""\n'
        "from omnigent_client import tool\n\n\n"
        "@tool\n"
        "def demo(value: str) -> str:\n"
        '    """Demo tool."""\n'
        "    return value\n"
    )
    info = LocalToolInfo(name="demo", path="tools/python/demo.py", language="python")
    return load_local_python_tools([info], tmp_path)[0]


def test_module_path_returns_absolute_string(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    assert tool.module_path().endswith("tools/python/demo.py")


def test_local_python_tool_description_classmethod() -> None:
    assert LocalPythonTool.description() == "Custom local Python tool."


def test_invoke_passes_state_root_when_workspace_present(tmp_path: Path) -> None:
    ctx = ToolContext(
        task_id="task_test",
        agent_id="agent_x",
        workspace=tmp_path,
    )
    tool = _make_tool(tmp_path)
    result = tool.invoke(json.dumps({"value": "with-state"}), ctx)
    assert "with-state" in result
    state_dir = tmp_path / ".tool_state" / "agent_x"
    assert state_dir.is_dir()


def test_build_command_uv_wraps_inner_python_with_srt(tmp_path: Path) -> None:
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "deps.py").write_text(
        "# /// script\n# dependencies = ['requests>=2.0']\n# ///\n"
        '"""Deps."""\nfrom omnigent_client import tool\n\n\n'
        "@tool\n"
        "def deps(value: str) -> str:\n"
        '    """Doc."""\n'
        "    return value\n"
    )
    info = LocalToolInfo(
        name="deps",
        path="tools/python/deps.py",
        language="python",
        has_inline_deps=True,
        inline_deps=["requests>=2.0"],
    )
    tool = load_local_python_tools(
        [info],
        tmp_path,
        srt_available=True,
        uv_available=True,
        sandbox_enabled=True,
    )[0]
    cmd = tool._build_command(state_root="/tmp/state")
    assert cmd[:2] == ["uv", "run"]
    assert "srt" in cmd
    assert "python" in " ".join(cmd)


def test_write_srt_state_settings_whitelists_state_root(tmp_path: Path) -> None:
    state_root = str(tmp_path / "state")
    settings_path = _write_srt_settings_file(state_root)
    try:
        data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        assert data["filesystem"]["allowWrite"] == [state_root]
    finally:
        Path(settings_path).unlink(missing_ok=True)


@pytest.mark.parametrize(
    "raw,returncode,stderr,expected_fragment",
    [
        (b"", 1, b"stderr-msg", "exited with code 1"),
        (b"", 0, b"", "produced no response"),
        (b"not-json", 0, b"", "invalid JSON"),
        (json.dumps({"error": "boom"}).encode(), 0, b"", "Error: boom"),
    ],
)
def test_read_fd3_response_error_branches(
    raw: bytes,
    returncode: int,
    stderr: bytes,
    expected_fragment: str,
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        if raw:
            os.write(write_fd, raw)
        os.close(write_fd)
        result = _read_fd3_response(read_fd, returncode, stderr)
        assert expected_fragment in result
    finally:
        os.close(read_fd)


def test_read_stdout_response_branches() -> None:
    assert "exited with code 2" in _read_stdout_response(b"", 2, b"err")
    assert "produced no stdout" in _read_stdout_response(b"", 0, b"")
    bad_line = f"{_STDOUT_RESPONSE_PREFIX}not-json\n"
    assert "invalid JSON" in _read_stdout_response(bad_line.encode(), 0, b"")
    err_line = f"{_STDOUT_RESPONSE_PREFIX}{json.dumps({'error': 'nope'})}\n"
    assert _read_stdout_response(err_line.encode(), 0, b"") == "Error: nope"
    ok_line = f"{_STDOUT_RESPONSE_PREFIX}{json.dumps({'result': 'ok'})}\n"
    assert _read_stdout_response(ok_line.encode(), 0, b"") == "ok"
    assert "no recognized response" in _read_stdout_response(b"garbage\n", 1, b"e")


def test_scan_inline_metadata_oserror_is_noop(tmp_path: Path) -> None:
    info = LocalToolInfo(name="x", path="tools/python/x.py", language="python")
    _scan_inline_metadata(info, tmp_path / "missing.py")
    assert info.has_inline_deps is False
    assert info.inline_deps is None


def test_import_tool_module_raises_when_spec_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_path = tmp_path / "broken.py"
    tool_path.write_text("def ok() -> None:\n    pass\n")
    monkeypatch.setattr(
        "importlib.util.spec_from_file_location",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(LocalToolLoadError, match="cannot create module spec"):
        _import_tool_module(agent_name="demo", tool_path=tool_path)


def test_invoke_subprocess_closes_write_fd_on_popen_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _make_tool(tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr("omnigent.tools.local.subprocess.Popen", _boom)
    with pytest.raises(OSError, match="spawn failed"):
        tool._invoke_subprocess(["python"], b"{}", workspace=None)


def test_invoke_stdout_sets_workspace_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _make_tool(tmp_path)
    captured: dict[str, str] = {}

    def _fake_popen(*_args, **kwargs):
        captured.update(kwargs.get("env", {}))
        proc = MagicMock()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0
        return proc

    monkeypatch.setattr("omnigent.tools.local.subprocess.Popen", _fake_popen)
    monkeypatch.setattr(
        "omnigent.tools.local._read_stdout_response",
        lambda *_args, **_kwargs: "ok",
    )
    tool._invoke_stdout(["python"], b"{}", workspace=tmp_path)
    assert captured["_AP_WORKSPACE"] == str(tmp_path)
    assert captured["_AP_RESPONSE_MODE"] == "stdout"


def test_invoke_subprocess_sets_workspace_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _make_tool(tmp_path)
    captured: dict[str, str] = {}

    def _fake_popen(*_args, **kwargs):
        captured.update(kwargs.get("env", {}))
        proc = MagicMock()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0
        return proc

    monkeypatch.setattr("omnigent.tools.local.subprocess.Popen", _fake_popen)
    monkeypatch.setattr(
        "omnigent.tools.local._read_fd3_response",
        lambda *_args, **_kwargs: "ok",
    )
    ctx = ToolContext(task_id="t", agent_id="a", workspace=tmp_path)
    tool._invoke_subprocess(["python"], b"{}", workspace=tmp_path)
    assert captured["_AP_WORKSPACE"] == str(tmp_path)