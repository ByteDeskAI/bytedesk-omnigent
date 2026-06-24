"""Batch-26 coverage for repl tmux pane integration and session log helpers."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl._session_log import (
    _build_node_async,
    _build_node_sync,
    _extract_child_conversation_ids,
    _fetch_all_items_sync,
    _fetch_all_items_via_sessions,
    collect_log_files,
    default_log_zip_path,
    write_logs_zip,
    write_session_log,
)
from omnigent.repl._tmux_pane import (
    _discover_split_bindings,
    _list_prefix_keys,
    _parse_bind_line,
    _resolve_omnigent_argv,
    _tmux_version_ok,
    _unwrap_existing_wrapper,
    _user_args_after_launcher,
    read_pane_option,
    register_pane,
    update_conv_id,
)


# ── tmux pane helpers ────────────────────────────────────────


def test_tmux_version_ok_returns_false_when_tmux_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert _tmux_version_ok() is False


def test_tmux_version_ok_returns_false_for_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"stdout": "not-tmux-output", "returncode": 0})(),
    )
    assert _tmux_version_ok() is False


def test_resolve_argv_uses_python_m_when_argv0_is_cli_py(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["/repo/omnigent/cli.py", "run"])
    monkeypatch.setattr("sys.executable", "/usr/bin/python3")
    assert _resolve_omnigent_argv() == ["/usr/bin/python3", "-m", "omnigent.cli"]


def test_user_args_after_launcher_returns_empty_when_no_subcommand() -> None:
    assert _user_args_after_launcher(["python", "-m", "omnigent.cli"]) == []


def test_list_prefix_keys_returns_empty_on_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("tmux unavailable")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert _list_prefix_keys() == []


def test_parse_bind_line_skips_single_char_flags_and_returns_none_when_no_key() -> None:
    assert _parse_bind_line("bind-key -T prefix -r") is None


def test_unwrap_existing_wrapper_rejects_wrong_marker() -> None:
    tokens = [
        "if-shell",
        "-F",
        "#{?#{@other-option},1,0}",
        "run-shell chooser",
        "split-window -v",
    ]
    assert _unwrap_existing_wrapper(tokens) is None


def test_unwrap_existing_wrapper_rejects_non_if_shell_wrappers() -> None:
    tokens = ["run-shell", "-F", "#{?#{@omnigent-conv-id},1,0}", "true", "split-window -v"]
    assert _unwrap_existing_wrapper(tokens) is None


def test_unwrap_existing_wrapper_returns_none_on_shlex_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shlex

    def _raise(_value: str) -> list[str]:
        raise ValueError("no closing quotation")

    monkeypatch.setattr(shlex, "split", _raise)
    tokens = ["if-shell", "-F", "#{?#{@omnigent-conv-id},1,0}", "true", "split-window -v"]
    assert _unwrap_existing_wrapper(tokens) is None


def test_discover_split_bindings_skips_unparseable_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_output = 'bind-key -T prefix x split-window -c "unclosed'
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"stdout": fake_output, "returncode": 0})(),
    )
    assert _discover_split_bindings() == []


def test_register_pane_no_op_when_integration_enabled_outside_tmux(
    _no_tmux: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.repl._tmux_pane.PANE_INTEGRATION_ENABLED", True)
    captured: list[list[str]] = []

    def _capture(cmd: list[str], **_kwargs: object) -> Any:
        captured.append(cmd)
        return type("R", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(subprocess, "run", _capture)
    register_pane(
        conv_id="conv_x",
        agent_name="agent",
        agent_yaml=None,
        launch_argv=["omnigent", "run"],
        server_url=None,
    )
    assert captured == []


def test_register_pane_warns_when_tmux_pane_unset_with_integration_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("omnigent.repl._tmux_pane.PANE_INTEGRATION_ENABLED", True)
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: type("R", (), {"stdout": "", "returncode": 0})())

    register_pane(
        conv_id="conv_x",
        agent_name="agent",
        agent_yaml=None,
        launch_argv=["omnigent", "run"],
        server_url=None,
    )
    assert any("TMUX_PANE" in record.message for record in caplog.records)


def test_update_conv_id_sets_option_inside_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%2")
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **_k: captured.append(cmd) or type("R", (), {"returncode": 0})())

    update_conv_id("conv_updated")
    assert captured == [
        ["tmux", "set-option", "-p", "-t", "%2", "@omnigent-conv-id", "conv_updated"]
    ]


def test_update_conv_id_no_op_outside_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **_k: captured.append(cmd) or type("R", (), {"returncode": 0})())

    update_conv_id("conv_updated")
    assert captured == []


def test_update_conv_id_no_op_when_tmux_pane_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **_k: captured.append(cmd) or type("R", (), {"returncode": 0})())

    update_conv_id("conv_updated")
    assert captured == []


def test_read_pane_option_returns_value_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"stdout": "conv_abc\n", "returncode": 0})(),
    )
    assert read_pane_option("%0", "@omnigent-conv-id") == "conv_abc"


def test_read_pane_option_returns_none_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "tmux")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert read_pane_option("%0", "@omnigent-conv-id") is None


# ── session log zip helpers ──────────────────────────────────


def test_default_log_zip_path_includes_session_slug(tmp_path: Path) -> None:
    path = default_log_zip_path(tmp_path, session_id="conv_abc123")
    assert path.parent == tmp_path
    assert path.name.startswith("omnigent-logs-conv_abc123-")


def test_default_log_zip_path_without_session_id(tmp_path: Path) -> None:
    path = default_log_zip_path(tmp_path, session_id=None)
    assert path.name.startswith("omnigent-logs-")
    assert "conv_" not in path.name


def test_collect_log_files_skips_missing_zip_and_directories(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    missing = logs / "missing.log"
    (logs / "bundle.zip").write_text("zip", encoding="utf-8")
    current = logs / "current.log"
    current.write_text("ok\n", encoding="utf-8")

    entries = collect_log_files([missing, logs, current, logs / "bundle.zip"])
    assert entries == [(current.resolve(), "logs/current.log")]


def test_collect_log_files_dedupes_and_disambiguates_arcnames(tmp_path: Path) -> None:
    logs_a = tmp_path / "a" / "logs"
    logs_b = tmp_path / "b" / "logs"
    logs_a.mkdir(parents=True)
    logs_b.mkdir(parents=True)
    first = logs_a / "dup.log"
    second = logs_b / "dup.log"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")

    entries = collect_log_files([first, second])
    arcnames = [arc for _, arc in entries]
    assert len(entries) == 2
    assert arcnames.count("logs/dup.log") == 1
    assert sum(1 for name in arcnames if name.startswith("logs/") and name.endswith("dup.log")) == 2


def test_write_logs_zip_creates_empty_bundle_when_no_files(tmp_path: Path) -> None:
    target = tmp_path / "empty.zip"
    zip_path, count = write_logs_zip(target, log_paths=[tmp_path / "missing.log"])
    assert zip_path == target
    assert count == 0
    with zipfile.ZipFile(target) as zf:
        assert zf.namelist() == []


def test_write_logs_zip_skips_adding_itself_as_member(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    other = logs / "other.log"
    other.write_text("other\n", encoding="utf-8")
    target = logs / "bundle.log"
    target.write_text("seed\n", encoding="utf-8")
    count_path, count = write_logs_zip(
        target,
        log_paths=[other, target],
        session_id="sess_001",
    )
    assert count_path == target
    assert count == 1
    with zipfile.ZipFile(target) as zf:
        assert zf.namelist() == ["logs/other.log"]


def test_write_logs_zip_builds_default_output_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.repl._session_log.default_log_zip_path",
        lambda **_kwargs: tmp_path / "auto.zip",
    )
    zip_path, count = write_logs_zip(log_paths=[tmp_path / "missing.log"], session_id="sess_x")
    assert zip_path == tmp_path / "auto.zip"
    assert count == 0


def test_write_logs_zip_tolerates_target_resolve_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    log_file = logs / "session.log"
    log_file.write_text("payload\n", encoding="utf-8")
    target = tmp_path / "bundle.zip"
    real_resolve = Path.resolve

    def _resolve_raises(self: Path, *, strict: bool) -> Path:
        if self == target and strict is False:
            raise OSError("target resolve failed")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _resolve_raises)
    zip_path, count = write_logs_zip(target, log_paths=[log_file])
    assert zip_path == target
    assert count == 1


def test_write_logs_zip_skips_members_that_fail_resolve_in_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    good = logs / "good.log"
    bad = logs / "bad.log"
    good.write_text("ok\n", encoding="utf-8")
    bad.write_text("bad\n", encoding="utf-8")
    target = tmp_path / "bundle.zip"
    real_resolve = Path.resolve
    strict_calls: dict[Path, int] = {}

    def _resolve(self: Path, *, strict: bool) -> Path:
        if strict:
            strict_calls[self] = strict_calls.get(self, 0) + 1
            if self == bad and strict_calls[self] > 1:
                raise OSError("member resolve failed")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _resolve)
    zip_path, count = write_logs_zip(target, log_paths=[good, bad])
    assert zip_path == target
    assert count == 1


# ── session log tree walking ─────────────────────────────────


def test_extract_child_conversation_ids_reads_nested_entity_shape() -> None:
    handle = {"kind": "sub_agent", "conversation_id": "conv_child_1"}
    items = [
        {
            "type": "function_call_output",
            "data": {"output": json.dumps(handle)},
        }
    ]
    assert _extract_child_conversation_ids(items) == ["conv_child_1"]


def test_extract_child_conversation_ids_skips_invalid_handles() -> None:
    items = [
        {"type": "function_call_output", "output": '{"kind":"other"}'},
        {"type": "function_call_output", "data": {"output": 123}},
        {"type": "message", "data": {"role": "user"}},
    ]
    assert _extract_child_conversation_ids(items) == []


def test_build_node_sync_skips_child_already_in_visited_set(db_uri: str) -> None:
    from omnigent.entities import FunctionCallOutputData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(title="parent")
    child = store.create_conversation(title="child")
    handle = {
        "task_id": "tsk_skip",
        "conversation_id": child.id,
        "kind": "sub_agent",
        "type": "worker",
        "name": "worker",
        "status": "in_progress",
    }
    store.append(
        parent.id,
        [
            NewConversationItem(
                type="function_call_output",
                response_id="resp_spawn",
                data=FunctionCallOutputData(call_id="call_1", output=json.dumps(handle)),
            )
        ],
    )
    visited: set[str] = {child.id}
    node = _build_node_sync(store, parent.id, visited)
    assert node["children"] == []


def test_build_node_sync_emits_cycle_stub(db_uri: str) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="cycle")
    visited: set[str] = {conv.id}
    node = _build_node_sync(store, conv.id, visited)
    assert node == {
        "id": conv.id,
        "cycle": True,
        "items": [],
        "children": [],
    }


def test_fetch_all_items_sync_stops_when_last_id_empty() -> None:
    class _Item:
        def __init__(self, item_id: str | None) -> None:
            self.id = item_id

        def model_dump(self) -> dict[str, object]:
            return {"id": self.id}

    class _Page:
        def __init__(self, data: list[_Item]) -> None:
            self.data = data

    rows = [_Item(f"item_{i}") for i in range(99)]
    rows.append(_Item(None))

    class _Store:
        def list_items(self, **_kwargs: object) -> _Page:
            return _Page(rows)

    items = _fetch_all_items_sync(_Store(), "conv_test")
    assert len(items) == 100


def test_fetch_all_items_sync_returns_early_on_short_page() -> None:
    class _Item:
        def __init__(self, item_id: str) -> None:
            self.id = item_id

        def model_dump(self) -> dict[str, object]:
            return {"id": self.id}

    class _Page:
        def __init__(self, data: list[_Item]) -> None:
            self.data = data

    class _Store:
        def list_items(self, **_kwargs: object) -> _Page:
            return _Page([_Item("only")])

    assert _fetch_all_items_sync(_Store(), "conv_test") == [{"id": "only"}]


def test_collect_log_files_skips_duplicate_paths(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "dup.log"
    log_file.parent.mkdir()
    log_file.write_text("one\n", encoding="utf-8")
    entries = collect_log_files([log_file, log_file])
    assert entries == [(log_file.resolve(), "logs/dup.log")]


def test_collect_log_files_skips_unresolvable_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_file = tmp_path / "logs" / "current.log"
    log_file.parent.mkdir()
    log_file.write_text("ok\n", encoding="utf-8")
    real_resolve = Path.resolve

    def _resolve_raises(self: Path, *, strict: bool) -> Path:
        if self == log_file:
            raise OSError("resolve failed")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _resolve_raises)
    assert collect_log_files([log_file]) == []


@pytest.mark.asyncio
async def test_fetch_all_items_via_sessions_returns_empty_when_first_page_missing() -> None:
    sessions = MagicMock()
    sessions.list_items = AsyncMock(return_value=[])
    client = MagicMock(sessions=sessions)
    assert await _fetch_all_items_via_sessions(client, "sess_empty") == []


@pytest.mark.asyncio
async def test_fetch_all_items_via_sessions_stops_when_last_id_invalid() -> None:
    sessions = MagicMock()
    sessions.list_items = AsyncMock(return_value=[{"id": 123}])
    client = MagicMock(sessions=sessions)
    items = await _fetch_all_items_via_sessions(client, "sess_bad")
    assert items == [{"id": 123}]


@pytest.mark.asyncio
async def test_fetch_all_items_via_sessions_paginates_and_stops_on_short_page() -> None:
    pages = [
        [{"id": f"item_{i}"} for i in range(100)],
        [{"id": "item_100"}],
    ]

    class _Sessions:
        def __init__(self) -> None:
            self.calls = 0

        async def list_items(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            page = pages[self.calls]
            self.calls += 1
            return page

    client = SimpleNamespace(sessions=_Sessions())
    items = await _fetch_all_items_via_sessions(client, "sess_1")  # type: ignore[arg-type]
    assert len(items) == 101
    assert client.sessions.calls == 2


@pytest.mark.asyncio
async def test_build_node_async_emits_cycle_stub() -> None:
    client = MagicMock()
    node = await _build_node_async(client, "conv_cycle", {"conv_cycle"})
    assert node["cycle"] is True
    client.sessions.get.assert_not_called()


@pytest.mark.asyncio
async def test_build_node_async_skips_child_already_in_visited_set() -> None:
    handle = {
        "kind": "sub_agent",
        "conversation_id": "conv_child",
        "type": "worker",
        "name": "worker",
        "status": "in_progress",
    }
    parent_items = [
        {
            "id": "item_spawn",
            "type": "function_call_output",
            "output": json.dumps(handle),
        }
    ]
    parent_snap = SimpleNamespace(
        id="conv_parent",
        title="parent",
        created_at=1,
        labels={},
    )
    sessions = MagicMock()
    sessions.get = AsyncMock(return_value=parent_snap)
    sessions.list_items = AsyncMock(return_value=parent_items)
    client = MagicMock(sessions=sessions)

    node = await _build_node_async(client, "conv_parent", {"conv_child"})
    assert node["children"] == []


@pytest.mark.asyncio
async def test_write_session_log_via_sdk_writes_ap_native_shape(
    tmp_path: Path,
) -> None:
    handle = {
        "kind": "sub_agent",
        "conversation_id": "conv_child",
        "type": "worker",
        "name": "worker",
        "status": "in_progress",
    }
    parent_items = [
        {"id": "item_1", "type": "message", "role": "user"},
        {
            "id": "item_2",
            "type": "function_call_output",
            "output": json.dumps(handle),
        },
    ]
    child_items = [{"id": "item_c1", "type": "message", "role": "assistant"}]
    parent_snap = SimpleNamespace(
        id="conv_parent",
        title="parent",
        created_at=1714248083,
        labels={"env": "test"},
    )
    child_snap = SimpleNamespace(
        id="conv_child",
        title="child",
        created_at=1714248084,
        labels={},
    )

    sessions = MagicMock()
    sessions.get = AsyncMock(side_effect=[parent_snap, child_snap])
    sessions.list_items = AsyncMock(side_effect=[parent_items, child_items])
    client = MagicMock(sessions=sessions)

    path = await write_session_log(
        client,
        "conv_parent",
        agent_name="sdk_agent",
        log_dir=tmp_path,
    )
    payload = json.loads(path.read_text())
    assert payload["agent_name"] == "sdk_agent"
    assert payload["conversation"]["id"] == "conv_parent"
    assert len(payload["conversation"]["children"]) == 1
    assert payload["conversation"]["children"][0]["id"] == "conv_child"


@pytest.fixture()
def _no_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)