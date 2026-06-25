"""Edge tests for pure REPL helper functions in ``_repl.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.onboarding.provider_config import SUBSCRIPTION_KIND
from omnigent.repl._repl import (
    _ApprovalVerdict,
    _display_cwd,
    _header_glyph,
    _humanize_agent_name,
    _is_remote_server_url,
    _parse_approval_input,
)


def test_display_cwd_collapses_home_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    work = home / "omnigent"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    assert _display_cwd() == "~/omnigent"


def test_display_cwd_returns_tilde_for_home_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert _display_cwd() == "~"


def test_display_cwd_returns_absolute_path_outside_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(outside)
    assert _display_cwd() == str(outside.resolve())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (None, False),
        ("", False),
        ("http://127.0.0.1:6767", False),
        ("http://localhost:6767", False),
        ("https://example.databricks.com", True),
        ("http://10.0.0.5:8080", True),
    ],
)
def test_is_remote_server_url(url: str | None, expected: bool) -> None:
    assert _is_remote_server_url(url) is expected


@pytest.mark.parametrize(
    ("agent_name", "expected"),
    [
        ("resume_test", "resume test"),
        ("my-agent", "my agent"),
        ("plain", "plain"),
    ],
)
def test_humanize_agent_name(agent_name: str, expected: str) -> None:
    assert _humanize_agent_name(agent_name) == expected


def test_header_glyph_suppresses_subscription_ticket() -> None:
    assert _header_glyph(SUBSCRIPTION_KIND) == ""


def test_header_glyph_returns_kind_glyph_for_key() -> None:
    from omnigent.onboarding.configure_models import kind_glyph

    assert _header_glyph("key") == kind_glyph("key")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("y", _ApprovalVerdict.APPROVE_ONCE),
        ("YES", _ApprovalVerdict.APPROVE_ONCE),
        ("approve", _ApprovalVerdict.APPROVE_ONCE),
        ("a", _ApprovalVerdict.APPROVE_ALWAYS),
        ("always", _ApprovalVerdict.APPROVE_ALWAYS),
        ("approve always", _ApprovalVerdict.APPROVE_ALWAYS),
        ("n", _ApprovalVerdict.REFUSE),
        ("maybe", _ApprovalVerdict.REFUSE),
        ("  yes  ", _ApprovalVerdict.APPROVE_ONCE),
    ],
)
def test_parse_approval_input(text: str, expected: _ApprovalVerdict) -> None:
    assert _parse_approval_input(text) is expected
