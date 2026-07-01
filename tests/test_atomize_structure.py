"""Structural atomization gate — verifies page/shell entry files stay thin."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AP_WEB = REPO_ROOT / "ap-web" / "src"

# Priority-1 thresholds from bytedesk-atomize survey heuristics (adapted for ap-web).
MAX_PAGE_ENTRY_LINES = 150
MAX_SHELL_ENTRY_LINES = 300


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _page_entry_files() -> list[Path]:
    pages = AP_WEB / "pages"
    return sorted(p for p in pages.glob("*.tsx") if not p.name.endswith(".test.tsx"))


def _shell_entry_files() -> list[Path]:
    shell = AP_WEB / "shell"
    return sorted(
        p
        for p in shell.glob("*.tsx")
        if not p.name.endswith(".test.tsx") and p.parent == shell
    )


def _inline_component_count_in_pages() -> int:
    import re

    pattern = re.compile(r"^(function |const \w+ = \()", re.MULTILINE)
    count = 0
    for page in _page_entry_files():
        count += len(pattern.findall(page.read_text(encoding="utf-8")))
    return count


def test_page_entry_files_under_atomize_threshold() -> None:
    offenders = [
        (p.name, _line_count(p))
        for p in _page_entry_files()
        if _line_count(p) > MAX_PAGE_ENTRY_LINES
    ]
    assert offenders == [], f"Page entry files exceed {MAX_PAGE_ENTRY_LINES} lines: {offenders}"


def test_shell_entry_files_under_atomize_threshold() -> None:
    offenders = [
        (p.name, _line_count(p))
        for p in _shell_entry_files()
        if _line_count(p) > MAX_SHELL_ENTRY_LINES
    ]
    assert offenders == [], f"Shell entry files exceed {MAX_SHELL_ENTRY_LINES} lines: {offenders}"


def test_no_inline_components_in_page_entries() -> None:
    assert _inline_component_count_in_pages() == 0


def test_decomposed_python_facades_importable() -> None:
    from omnigent.server.routes import sessions
    from omnigent.runner import app as runner_app
    from omnigent import cli

    assert sessions.create_sessions_router is not None
    assert runner_app.create_runner_app is not None
    assert cli.cli is not None