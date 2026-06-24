"""Edge-case coverage for omnigent.runtime.filesystem_registry."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnigent.runtime.filesystem_registry import (
    AgentEditFilesystemRegistry,
    GitFilesystemRegistry,
    _FileEvent,
    _unquote_git_path,
)


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def test_unquote_git_path_falls_back_on_unknown_escape() -> None:
    """Unknown escape sequences keep the backslash literally."""
    assert _unquote_git_path(r"foo\zbar") == r"foo\zbar"


def test_filesystem_registry_base_defaults_are_no_ops(tmp_path: Path) -> None:
    """GitFilesystemRegistry inherits no-op defaults from the abstract base."""
    (tmp_path / ".git").mkdir()
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    assert reg.cwd == tmp_path.resolve()
    reg.record_change("ignored.py", "created", "conv_1")
    reg.seed_snapshot("ignored.py", "content", session_id="conv_1")
    reg.unregister_conversation("conv_1")
    reg.start()
    reg.stop()


def test_record_change_ignores_paths_outside_workspace(
    registry: AgentEditFilesystemRegistry,
) -> None:
    """Paths that escape the workspace are not recorded."""
    registry.record_change("/etc/passwd", "modified", "conv_escape")
    assert registry.list_changed_files("conv_escape", limit=10) == []


def test_record_change_captures_file_stat_metadata(
    tmp_path: Path,
) -> None:
    """Existing files contribute size and mtime to change records."""
    reg = AgentEditFilesystemRegistry(watch_path=tmp_path)
    target = tmp_path / "tracked.py"
    target.write_text("hello")

    reg.record_change("tracked.py", "modified", "conv_stat")

    results = reg.list_changed_files("conv_stat", limit=10)
    assert len(results) == 1
    assert results[0]["bytes"] == len("hello")
    assert results[0]["modified_at"] is not None


def test_unregister_conversation_evicts_snapshots(tmp_path: Path) -> None:
    """Session teardown removes seeded snapshot baselines."""
    reg = AgentEditFilesystemRegistry(watch_path=tmp_path)
    reg.seed_snapshot("lib.py", "original", session_id="conv_drop")
    reg.unregister_conversation("conv_drop")
    assert reg.get_baseline("lib.py") is None


def test_list_changed_files_filters_ephemeral_events_injected_directly(
    registry: AgentEditFilesystemRegistry,
) -> None:
    """Ephemeral artifacts are hidden even when injected without record_change."""
    conv_id = "conv_ephemeral"
    with registry._lock:
        registry._session_events.setdefault(conv_id, []).append(
            _FileEvent(
                path="scratch.tmp",
                operation="created",
                timestamp=time.time(),
                bytes=None,
                modified_at=None,
            )
        )

    assert registry.list_changed_files(conv_id, limit=10) == []


def test_get_changed_file_returns_net_status(registry: AgentEditFilesystemRegistry) -> None:
    """Single-path lookup mirrors list_changed_files merge semantics."""
    conv_id = "conv_single"
    registry.record_change("foo.py", "created", conv_id)
    registry.record_change("foo.py", "modified", conv_id)

    result = registry.get_changed_file(conv_id, "foo.py")
    assert result is not None
    assert result["status"] == "created"
    assert result["path"] == "foo.py"


def test_get_changed_file_returns_none_for_invalid_path(
    registry: AgentEditFilesystemRegistry,
) -> None:
    """Escaped paths are rejected by get_changed_file."""
    registry.record_change("inside.py", "created", "conv_invalid")
    assert registry.get_changed_file("conv_invalid", "/outside/inside.py") is None


def test_get_changed_file_hides_created_then_deleted(
    registry: AgentEditFilesystemRegistry,
) -> None:
    """Files created and deleted within one session have no net change."""
    conv_id = "conv_cycle"
    registry.record_change("temp.py", "created", conv_id)
    registry.record_change("temp.py", "deleted", conv_id)
    assert registry.get_changed_file(conv_id, "temp.py") is None


def test_get_baseline_and_seed_snapshot_ignore_invalid_paths(
    registry: AgentEditFilesystemRegistry,
) -> None:
    """Snapshot helpers reject paths outside the workspace."""
    assert registry.get_baseline("/etc/passwd") is None
    registry.seed_snapshot("/etc/passwd", "secret", session_id="conv_bad")
    assert registry.get_baseline("/etc/passwd") is None


def test_seed_snapshot_registers_path_under_session_id(tmp_path: Path) -> None:
    """Snapshots seeded with a session id are tracked for later eviction."""
    reg = AgentEditFilesystemRegistry(watch_path=tmp_path)
    reg.seed_snapshot("scoped.py", "v1", session_id="conv_scope")
    assert reg._snapshot_sessions["conv_scope"] == {"scoped.py"}


def test_git_list_changed_files_returns_empty_when_git_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess failures degrade to an empty change list."""
    (tmp_path / ".git").mkdir()
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert reg.list_changed_files("conv", limit=10) == []


def test_git_list_changed_files_returns_empty_on_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero git status exit codes yield no records."""
    (tmp_path / ".git").mkdir()
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    def _fail(*_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 128
        result.stdout = b""
        return result

    monkeypatch.setattr(subprocess, "run", _fail)
    assert reg.list_changed_files("conv", limit=10) == []


def test_git_list_changed_files_skips_malformed_and_out_of_scope_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed porcelain lines and out-of-scope paths are ignored."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / ".git").mkdir()
    reg = GitFilesystemRegistry(watch_path=workspace, git_root=tmp_path)

    stdout = "\n".join(
        [
            "??",  # malformed — too short after status
            "?? sibling.py",  # under git root but outside watch_path
            "?? workspace/src.py",  # inside watch_path
        ]
    )

    def _mock_run(*_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = stdout.encode()
        return result

    monkeypatch.setattr(subprocess, "run", _mock_run)
    (workspace / "src.py").write_text("inside")

    results = reg.list_changed_files("conv", limit=10)
    assert [r["path"] for r in results] == ["src.py"]


def test_get_changed_file_returns_none_when_path_has_no_events(
    registry: AgentEditFilesystemRegistry,
) -> None:
    """Lookup returns None when the normalized path has no session events."""
    registry.record_change("other.py", "created", "conv_empty")
    assert registry.get_changed_file("conv_empty", "missing.py") is None


def test_git_get_changed_file_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed single-file lookup handles failures and successful parses."""
    (tmp_path / ".git").mkdir()
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    assert reg.get_changed_file("conv", "/outside/file.py") is None

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert reg.get_changed_file("conv", "missing.py") is None

    def _success(*_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = b"?? found.py"
        return result

    monkeypatch.setattr(subprocess, "run", _success)
    (tmp_path / "found.py").write_text("data")
    record = reg.get_changed_file("conv", "found.py")
    assert record is not None
    assert record["status"] == "created"
    assert record["path"] == "found.py"

    def _nonzero(*_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 128
        result.stdout = b""
        return result

    monkeypatch.setattr(subprocess, "run", _nonzero)
    assert reg.get_changed_file("conv", "found.py") is None

    def _malformed_then_empty(*_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = b"M \n"
        return result

    monkeypatch.setattr(subprocess, "run", _malformed_then_empty)
    assert reg.get_changed_file("conv", "found.py") is None

    def _empty_status(*_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = b""
        return result

    monkeypatch.setattr(subprocess, "run", _empty_status)
    assert reg.get_changed_file("conv", "found.py") is None


def test_git_get_baseline_returns_none_for_invalid_path(tmp_path: Path) -> None:
    """Baseline lookup rejects paths outside the workspace."""
    (tmp_path / ".git").mkdir()
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    assert reg.get_baseline("/outside/file.py") is None


def test_git_get_changed_file_returns_none_when_watch_path_not_under_git_root(
    tmp_path: Path,
) -> None:
    """Lookup is impossible when the workspace is outside the git root."""
    git_root = tmp_path / "repo"
    git_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    reg = GitFilesystemRegistry(watch_path=outside, git_root=git_root)
    assert reg.get_changed_file("conv", "file.py") is None


def test_git_get_baseline_returns_none_when_watch_path_not_under_git_root(
    tmp_path: Path,
) -> None:
    """Baseline lookup requires the workspace to live inside the git root."""
    git_root = tmp_path / "repo"
    git_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    reg = GitFilesystemRegistry(watch_path=outside, git_root=git_root)
    assert reg.get_baseline("file.py") is None


def test_git_get_baseline_logs_subprocess_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """git show failures degrade quietly to None."""
    env = _git_env()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("git show unavailable")

    monkeypatch.setattr(subprocess, "run", _raise)

    with caplog.at_level("DEBUG", logger="omnigent.runtime.filesystem_registry"):
        assert reg.get_baseline("file.py") is None

    assert any("git show failed" in record.message for record in caplog.records)


def test_git_to_rel_returns_none_for_paths_outside_watch_path(tmp_path: Path) -> None:
    """Paths under the git root but outside the workspace are ignored."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / ".git").mkdir()
    reg = GitFilesystemRegistry(watch_path=workspace, git_root=tmp_path)
    assert reg._git_to_rel("sibling.py") is None


def test_make_record_tolerates_missing_files(tmp_path: Path) -> None:
    """Deleted or missing files omit stat metadata instead of raising."""
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    record = reg._make_record("gone.py", "modified")
    assert record == {
        "path": "gone.py",
        "status": "modified",
        "bytes": None,
        "modified_at": None,
    }


@pytest.fixture
def registry(tmp_path: Path) -> AgentEditFilesystemRegistry:
    return AgentEditFilesystemRegistry(watch_path=tmp_path)