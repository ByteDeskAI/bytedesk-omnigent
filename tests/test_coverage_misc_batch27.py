"""Batch-27 coverage for resume picker helpers and navigation paths."""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.repl._resume_picker import (
    _PromptToolkitPickerState,
    _append_prompt_toolkit_item,
    _append_prompt_toolkit_metadata,
    _append_prompt_toolkit_preview,
    _collect_previews_async,
    _collect_previews_sync,
    _extract_text_from_content_blocks,
    _format_when,
    _is_tty,
    _launch_state_for_row,
    _last_message_preview_from_dicts,
    _last_message_preview_from_entities,
    _prompt_toolkit_key_bindings,
    _read_line_choice,
    _Preview,
    pick_conversation,
    pick_conversation_from_sdk,
)


@dataclass
class _Row:
    id: str
    title: str | None
    created_at: int
    labels: dict[str, str] | None = None


def _rows(n: int) -> list[_Row]:
    return [
        _Row(
            id=f"conv_{i:04d}",
            title=f"chat-{i}",
            created_at=1735689600 + i * 86400,
        )
        for i in range(1, n + 1)
    ]


def test_pick_conversation_line_buffered_down_then_enter_selects_next_row() -> None:
    conversations = _rows(3)
    out = io.StringIO()
    selected = pick_conversation(
        conversations,
        agent_name="resume_test",
        out=out,
        in_=io.StringIO("down\n\n"),
    )
    assert selected == conversations[1].id


def test_pick_conversation_line_buffered_up_moves_after_down() -> None:
    conversations = _rows(3)
    out = io.StringIO()
    selected = pick_conversation(
        conversations,
        agent_name="resume_test",
        out=out,
        in_=io.StringIO("down\nup\n\n"),
    )
    assert selected == conversations[0].id


def test_pick_conversation_line_buffered_up_is_noop_at_first_row() -> None:
    conversations = _rows(3)
    out = io.StringIO()
    selected = pick_conversation(
        conversations,
        agent_name="resume_test",
        out=out,
        in_=io.StringIO("up\n\n"),
    )
    assert selected == conversations[0].id


def test_pick_conversation_line_buffered_down_is_noop_on_last_row() -> None:
    conversations = _rows(2)
    out = io.StringIO()
    selected = pick_conversation(
        conversations,
        agent_name="resume_test",
        out=out,
        in_=io.StringIO("down\ndown\n\n"),
    )
    assert selected == conversations[1].id


def test_pick_conversation_line_buffered_prev_returns_to_first_page() -> None:
    conversations = _rows(15)
    out = io.StringIO()
    selected = pick_conversation(
        conversations,
        agent_name="resume_test",
        out=out,
        in_=io.StringIO("n\np\n\n"),
    )
    assert selected == conversations[0].id


def test_prompt_toolkit_picker_state_page_navigation() -> None:
    state = _PromptToolkitPickerState(
        conversations=_rows(25),
        agent_name="agent",
        previews=None,
        show_runtime=False,
        show_workspace=False,
        selected_index=12,
    )
    state.next_page()
    assert state.selected_index == 20
    state.previous_page()
    assert state.selected_index == 10


def test_prompt_toolkit_key_bindings_move_and_cancel() -> None:
    state = _PromptToolkitPickerState(
        conversations=_rows(3),
        agent_name="agent",
        previews=None,
        show_runtime=False,
        show_workspace=False,
    )
    bindings = _prompt_toolkit_key_bindings(state)

    class _App:
        def __init__(self) -> None:
            self.invalidated = False
            self.exit_args: tuple[Any, ...] = ()

        def invalidate(self) -> None:
            self.invalidated = True

        def exit(self, *args: object, **kwargs: object) -> None:
            self.exit_args = (args, kwargs)

    class _Event:
        def __init__(self) -> None:
            self.app = _App()

    event = _Event()
    for handler in bindings.bindings:
        if handler.keys == ("up",):
            handler.handler(event)
        if handler.keys == ("down",):
            handler.handler(event)
    assert state.selected_index == 1
    assert event.app.invalidated is True

    state.selected_index = 12
    for handler in bindings.bindings:
        if handler.keys == ("n",):
            handler.handler(event)
        if handler.keys == ("p",):
            handler.handler(event)

    for handler in bindings.bindings:
        if handler.keys == ("q",):
            handler.handler(event)
            break
    assert event.app.exit_args == ((), {"result": None})


def test_prompt_toolkit_key_binding_ctrl_c_raises_keyboard_interrupt() -> None:
    state = _PromptToolkitPickerState(
        conversations=_rows(1),
        agent_name="agent",
        previews=None,
        show_runtime=False,
        show_workspace=False,
    )
    bindings = _prompt_toolkit_key_bindings(state)

    class _App:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def exit(self, *args: object, **kwargs: object) -> None:
            self.kwargs = dict(kwargs)

    class _Event:
        def __init__(self) -> None:
            self.app = _App()

    event = _Event()
    for handler in bindings.bindings:
        if "c-c" not in str(handler.keys):
            continue
        handler.handler(event)
        assert event.app.kwargs.get("exception") is KeyboardInterrupt
        return
    pytest.fail("ctrl-c binding missing")


def test_append_prompt_toolkit_metadata_renders_workspace_and_runtime() -> None:
    conv = _Row(
        id="conv_workspace",
        title="workspace row",
        created_at=1735689600,
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )
    fragments: list[tuple[str, str]] = []
    with patch(
        "omnigent.repl._resume_picker._read_claude_launch_state",
        return_value=SimpleNamespace(working_directory="/tmp/other-dir"),
    ):
        _append_prompt_toolkit_metadata(
            fragments,
            conv,
            show_runtime=True,
            show_workspace=True,
            current_cwd=Path("/tmp/current-dir").resolve(),
        )
    joined = "".join(text for _, text in fragments)
    assert "/tmp/other-dir" in joined
    assert "↪" in joined and "cd" in joined
    assert "claude" in joined.lower() or "native" in joined.lower()


def test_append_prompt_toolkit_preview_placeholder_and_message() -> None:
    empty: list[tuple[str, str]] = []
    _append_prompt_toolkit_preview(empty, None)
    assert empty == [("class:muted", "    …\n")]

    filled: list[tuple[str, str]] = []
    _append_prompt_toolkit_preview(filled, _Preview(role="user", text="hello"))
    assert any("hello" in text for _, text in filled)


@pytest.mark.asyncio
async def test_collect_previews_async_maps_latest_messages() -> None:
    client = MagicMock()

    async def _list_items(conv_id: str, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"text": f"preview-{conv_id}"}],
            }
        ]

    client.sessions.list_items = AsyncMock(side_effect=_list_items)
    previews = await _collect_previews_async(client, _rows(2))
    assert previews["conv_0001"].text == "preview-conv_0001"
    assert previews["conv_0002"].role == "assistant"


@pytest.mark.asyncio
async def test_collect_previews_async_swallows_per_conversation_errors() -> None:
    client = MagicMock()
    client.sessions.list_items = AsyncMock(side_effect=RuntimeError("boom"))
    previews = await _collect_previews_async(client, _rows(1))
    assert previews["conv_0001"] is None


def test_collect_previews_sync_swallows_store_errors() -> None:
    store = MagicMock()
    store.list_items.side_effect = RuntimeError("store down")
    previews = _collect_previews_sync(store, _rows(1))
    assert previews["conv_0001"] is None


def test_last_message_preview_from_dicts_skips_non_dict_items() -> None:
    assert _last_message_preview_from_dicts(["not-a-dict"]) is None


def test_last_message_preview_from_dicts_skips_empty_role_and_meta() -> None:
    items = [
        {"type": "message", "role": "", "content": [{"text": "skip"}]},
        {"type": "message", "role": "user", "is_meta": True, "content": [{"text": "meta"}]},
        {"type": "message", "role": "user", "content": [{"text": "visible"}]},
    ]
    preview = _last_message_preview_from_dicts(items)
    assert preview is not None
    assert preview.text == "visible"


def test_last_message_preview_from_entities_skips_non_message_and_invalid_role() -> None:
    class _ToolItem:
        type = "function_call"

    class _BadRoleItem:
        type = "message"
        data = SimpleNamespace(is_meta=False, role=None, content=[{"text": "skip"}])

    assert _last_message_preview_from_entities([_ToolItem()]) is None
    assert _last_message_preview_from_entities([_BadRoleItem()]) is None


def test_extract_text_from_content_blocks_reads_object_blocks_and_empty_compact() -> None:
    class _Block:
        text = "object block"

    assert _extract_text_from_content_blocks([_Block()]) == "object block"
    assert _extract_text_from_content_blocks([{"text": "   \n\t  "}]) == ""


def test_append_prompt_toolkit_item_includes_preview_line() -> None:
    conv = _rows(2)[0]
    fragments: list[tuple[str, str]] = []
    _append_prompt_toolkit_item(
        fragments,
        conv,
        absolute_index=0,
        selected_index=0,
        previews={conv.id: _Preview(role="assistant", text="latest")},
        show_runtime=False,
        show_workspace=False,
        current_cwd=None,
        is_last=True,
    )
    assert any("latest" in text for _, text in fragments)


def test_is_tty_returns_false_when_fileno_missing() -> None:
    class _NoFileno:
        def isatty(self) -> bool:
            return True

    assert _is_tty(_NoFileno()) is False


def test_extract_text_from_content_blocks_handles_none_and_plain_string() -> None:
    assert _extract_text_from_content_blocks(None) == ""
    assert _extract_text_from_content_blocks("plain") == "plain"
    assert _extract_text_from_content_blocks(123) == ""


def test_format_when_relative_and_absolute_branches() -> None:
    now = int(time.time())
    assert _format_when(now - 30) == "just now"
    assert _format_when(now - 120).endswith("m ago")
    assert _format_when(now - 7200).endswith("h ago")
    assert _format_when(now - 172800).endswith("d ago")
    assert _format_when(now - 30 * 86400).startswith("Jan") or " " in _format_when(now - 30 * 86400)


def test_read_line_choice_maps_blank_line_to_select() -> None:
    assert _read_line_choice(io.StringIO("\n")) == "select"
    assert _read_line_choice(io.StringIO()) is None


def test_is_tty_requires_isatty_and_fileno() -> None:
    assert _is_tty(io.StringIO()) is False

    class _Pipe:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            raise OSError("no fd")

    assert _is_tty(_Pipe()) is False


def test_launch_state_for_row_reads_supported_wrapper_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.repl._resume_picker._read_claude_launch_state",
        lambda _conv_id: SimpleNamespace(working_directory="/claude"),
    )
    row = _Row("conv_claude", "t", 1, labels={"omnigent.wrapper": "claude-code-native-ui"})
    state = _launch_state_for_row(row)
    assert state is not None
    assert state.working_directory == "/claude"

    bad_labels = _Row("conv_bad", "t", 1, labels="not-a-dict")  # type: ignore[arg-type]
    assert _launch_state_for_row(bad_labels) is None


@pytest.mark.asyncio
async def test_pick_conversation_from_sdk_fetches_and_runs_picker() -> None:
    rows = _rows(1)

    class _Sessions:
        async def list(self, **_kwargs: object) -> list[_Row]:
            return rows

        async def list_items(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

    client = SimpleNamespace(sessions=_Sessions())
    selected = await pick_conversation_from_sdk(
        client,
        agent_name="resume_test",
        agent_id="agent_1",
        out=io.StringIO(),
        in_=io.StringIO("\n"),
    )
    assert selected == rows[0].id