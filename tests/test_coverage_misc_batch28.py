"""Batch-28 coverage for theme picker OSC detection and raw-termios paths."""

from __future__ import annotations

import io
import os
from typing import Any
from unittest.mock import MagicMock

import pytest
from omnigent_ui_sdk.terminal._theme import DARK_THEME, LIGHT_THEME

from omnigent.repl._theme_picker import (
    _clear_picker,
    _detect_terminal_background,
    _read_raw_byte,
    _read_raw_byte_timeout,
    _redraw_picker,
    _term_width,
    startup_theme_picker,
)


def test_read_raw_byte_returns_none_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.repl._theme_picker.os.read", lambda _fd, _n: b"")
    assert _read_raw_byte(0) is None


def test_read_raw_byte_decodes_single_character(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.repl._theme_picker.os.read", lambda _fd, _n: b"k")
    assert _read_raw_byte(0) == "k"


def test_read_raw_byte_timeout_returns_none_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "select.select",
        lambda *_args, **_kwargs: ([], [], []),
    )
    assert _read_raw_byte_timeout(0) is None


def test_read_raw_byte_timeout_reads_ready_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("select.select", lambda *_args, **_kwargs: ([0], [], []))
    monkeypatch.setattr("omnigent.repl._theme_picker.os.read", lambda _fd, _n: b"j")
    assert _read_raw_byte_timeout(0) == "j"


def test_redraw_picker_tracks_line_count_and_overwrites_previous_frame() -> None:
    out = io.StringIO()
    prev_lines = [0]
    _redraw_picker(out, 0, width=60, prev_lines=prev_lines)
    first = out.getvalue()
    first_line_count = prev_lines[0]
    assert first_line_count > 0
    assert "dark mode" in first

    _redraw_picker(out, 1, width=60, prev_lines=prev_lines)
    second = out.getvalue()
    assert f"\033[{first_line_count}A" in second
    assert "\033[J" in second
    assert "light mode" in second


def test_clear_picker_noop_for_zero_lines() -> None:
    out = io.StringIO()
    _clear_picker(out, 0)
    assert out.getvalue() == ""


def test_clear_picker_moves_cursor_up_and_clears_screen() -> None:
    out = io.StringIO()
    _clear_picker(out, 5)
    assert out.getvalue() == "\033[5A\033[J"


def test_term_width_uses_terminal_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.repl._theme_picker.os.get_terminal_size",
        lambda: os.terminal_size((120, 40)),
    )
    assert _term_width() == 120


def test_term_width_clamps_to_minimum_and_falls_back_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.repl._theme_picker.os.get_terminal_size",
        lambda: os.terminal_size((20, 10)),
    )
    assert _term_width() == 40

    def _raise() -> os.terminal_size:
        raise OSError("no tty")

    monkeypatch.setattr("omnigent.repl._theme_picker.os.get_terminal_size", _raise)
    assert _term_width() == 80


def test_detect_terminal_background_returns_none_when_stdin_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert _detect_terminal_background() is None


def test_detect_terminal_background_returns_none_when_stdout_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert _detect_terminal_background() is None


def _patch_detect_tty_fds(monkeypatch: pytest.MonkeyPatch, *, fd: int = 0) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.fileno", lambda: fd)
    monkeypatch.setattr("sys.stdout.fileno", lambda: 1)


def test_detect_terminal_background_returns_none_when_tcgetattr_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios

    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr(
        "termios.tcgetattr",
        MagicMock(side_effect=termios.error),
    )
    assert _detect_terminal_background() is None


def test_detect_terminal_background_returns_none_on_select_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0, 0, 0])
    monkeypatch.setattr("termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("tty.setraw", lambda _fd: None)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.write", lambda *_args: 0)
    monkeypatch.setattr("select.select", lambda *_args, **_kwargs: ([], [], []))
    assert _detect_terminal_background() is None


def test_detect_terminal_background_classifies_dark_osc_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = b"\033]11;rgb:0000/0000/0000\033\\"

    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0, 0, 0])
    monkeypatch.setattr("termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("tty.setraw", lambda _fd: None)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.write", lambda *_args: 0)

    def _select(read_list: list[Any], *_args: Any, **_kwargs: Any) -> tuple[list[Any], list[Any], list[Any]]:
        if read_list:
            return (read_list, [], [])
        return ([], [], [])

    monkeypatch.setattr("select.select", _select)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.read", lambda _fd, _n: response)
    assert _detect_terminal_background() == "dark"


def test_detect_terminal_background_reads_multiple_chunks_until_terminator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [b"\033]11;rgb:ffff/ffff/", b"ffff\x07"]
    read_calls = {"count": 0}

    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0, 0, 0])
    monkeypatch.setattr("termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("tty.setraw", lambda _fd: None)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.write", lambda *_args: 0)

    def _select(read_list: list[Any], *_args: Any, **_kwargs: Any) -> tuple[list[Any], list[Any], list[Any]]:
        if read_list:
            return (read_list, [], [])
        return ([], [], [])

    def _read(_fd: int, _n: int) -> bytes:
        chunk = chunks[read_calls["count"]]
        read_calls["count"] += 1
        return chunk

    monkeypatch.setattr("select.select", _select)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.read", _read)
    assert _detect_terminal_background() == "light"


def test_detect_terminal_background_inner_loop_stops_on_followup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_calls = {"count": 0}

    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0, 0, 0])
    monkeypatch.setattr("termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("tty.setraw", lambda _fd: None)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.write", lambda *_args: 0)

    def _select(read_list: list[Any], *_args: Any, **_kwargs: Any) -> tuple[list[Any], list[Any], list[Any]]:
        select_calls["count"] += 1
        if select_calls["count"] == 1 and read_list:
            return (read_list, [], [])
        return ([], [], [])

    monkeypatch.setattr("select.select", _select)
    monkeypatch.setattr(
        "omnigent.repl._theme_picker.os.read",
        lambda _fd, _n: b"\033]11;rgb:0000/0000/0000",
    )
    assert _detect_terminal_background() is None


def test_detect_terminal_background_inner_loop_stops_on_empty_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_calls = {"count": 0}

    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0, 0, 0])
    monkeypatch.setattr("termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("tty.setraw", lambda _fd: None)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.write", lambda *_args: 0)
    monkeypatch.setattr("select.select", lambda read_list, *_a, **_k: (read_list, [], []))

    def _read(_fd: int, _n: int) -> bytes:
        read_calls["count"] += 1
        if read_calls["count"] == 1:
            return b"\033]11;rgb:0000/0000/"
        return b""

    monkeypatch.setattr("omnigent.repl._theme_picker.os.read", _read)
    assert _detect_terminal_background() is None


def test_detect_terminal_background_returns_none_on_oserror_during_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0, 0, 0])
    monkeypatch.setattr("termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("tty.setraw", lambda _fd: None)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.write", lambda *_args: 0)
    monkeypatch.setattr("select.select", lambda *_args, **_kwargs: ([0], [], []))

    def _raise(_fd: int, _n: int) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr("omnigent.repl._theme_picker.os.read", _raise)
    assert _detect_terminal_background() is None


def _patch_startup_tty(monkeypatch: pytest.MonkeyPatch, *, home: Any, detected: str | None) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.fileno", lambda: 0)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0, 0, 0])
    monkeypatch.setattr("termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr("tty.setcbreak", lambda _fd: None)
    monkeypatch.setattr(
        "omnigent.repl._theme_picker._detect_terminal_background",
        lambda: detected,
    )
    monkeypatch.setattr(
        "omnigent.repl._theme_picker.os.get_terminal_size",
        lambda *_args, **_kwargs: os.terminal_size((80, 24)),
    )


def _run_startup_with_key_script(
    monkeypatch: pytest.MonkeyPatch,
    *,
    home: Any,
    detected: str | None,
    blocking_reads: list[str | None],
    timeout_reads: list[str | None] | None = None,
) -> tuple[Any, str]:
    blocking = iter(blocking_reads)
    timeouts = iter(timeout_reads or [])

    monkeypatch.setattr(
        "omnigent.repl._theme_picker._read_raw_byte",
        lambda _fd: next(blocking, None),
    )
    monkeypatch.setattr(
        "omnigent.repl._theme_picker._read_raw_byte_timeout",
        lambda _fd, **_kwargs: next(timeouts, None),
    )
    out = io.StringIO()
    theme = startup_theme_picker(out=out)
    return theme, out.getvalue()


def test_startup_theme_picker_tty_enter_confirms_light_default(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="light")
    theme, rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="light",
        blocking_reads=["\r"],
    )
    assert theme is LIGHT_THEME
    assert "light mode" in rendered
    config = (tmp_path / ".omnigent" / "config.yaml").read_text(encoding="utf-8")
    assert "theme: light" in config


def test_startup_theme_picker_tty_escape_accepts_current_selection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="dark")
    theme, _rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="dark",
        blocking_reads=["\x1b"],
        timeout_reads=[None],
    )
    assert theme is DARK_THEME


def test_startup_theme_picker_tty_ctrl_c_accepts_current_selection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="light")
    theme, _rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="light",
        blocking_reads=["\x03"],
    )
    assert theme is LIGHT_THEME


def test_startup_theme_picker_tty_arrow_down_then_enter_selects_dark(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="light")
    theme, rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="light",
        blocking_reads=["\x1b", "\r"],
        timeout_reads=["[", "B"],
    )
    assert theme is DARK_THEME
    assert "dark mode" in rendered


def test_startup_theme_picker_tty_arrow_up_wraps_to_dark_from_light_default(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="light")
    theme, _rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="light",
        blocking_reads=["\x1b", "\r"],
        timeout_reads=["[", "A"],
    )
    assert theme is DARK_THEME


def test_startup_theme_picker_tty_vi_keys_navigate_selection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="light")
    theme, _rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="light",
        blocking_reads=["j", "k", "J", "K", "\r"],
    )
    assert theme is LIGHT_THEME


def test_startup_theme_picker_tty_ignores_unknown_escape_sequences(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="dark")
    theme, _rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="dark",
        blocking_reads=["\x1b", "\r"],
        timeout_reads=["[", "Z"],
    )
    assert theme is DARK_THEME


def test_startup_theme_picker_tty_eof_accepts_current_selection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="dark")
    theme, _rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="dark",
        blocking_reads=[None],
    )
    assert theme is DARK_THEME


def test_startup_theme_picker_tty_newline_confirms_selection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_startup_tty(monkeypatch, home=tmp_path, detected="dark")
    theme, _rendered = _run_startup_with_key_script(
        monkeypatch,
        home=tmp_path,
        detected="dark",
        blocking_reads=["\n"],
    )
    assert theme is DARK_THEME


def test_startup_theme_picker_tty_tcgetattr_error_falls_back_to_detected(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr(
        "omnigent.repl._theme_picker._detect_terminal_background",
        lambda: "dark",
    )
    monkeypatch.setattr(
        "termios.tcgetattr",
        MagicMock(side_effect=termios.error),
    )
    out = io.StringIO()
    theme = startup_theme_picker(out=out)
    assert theme is DARK_THEME
    config = (tmp_path / ".omnigent" / "config.yaml").read_text(encoding="utf-8")
    assert "theme: dark" in config


def test_detect_terminal_background_suppresses_tcsetattr_error_in_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios

    _patch_detect_tty_fds(monkeypatch)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0, 0, 0])

    def _tcsetattr_raises(*_args: Any) -> None:
        raise termios.error

    monkeypatch.setattr("termios.tcsetattr", _tcsetattr_raises)
    monkeypatch.setattr("tty.setraw", lambda _fd: None)
    monkeypatch.setattr("omnigent.repl._theme_picker.os.write", lambda *_args: 0)
    monkeypatch.setattr("select.select", lambda *_args, **_kwargs: ([], [], []))
    assert _detect_terminal_background() is None