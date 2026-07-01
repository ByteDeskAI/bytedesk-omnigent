"""Structural atomization gate — verifies thin entry files across ap-web and Python facades."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AP_WEB = REPO_ROOT / "ap-web" / "src"

MAX_PAGE_ENTRY_LINES = 150
MAX_PAGE_ORGANISM_LINES = 250
MAX_SHELL_ENTRY_LINES = 300
MAX_COMPONENT_LINES = 500
MAX_AI_ELEMENT_ENTRY_LINES = 250
MAX_PYTHON_MODULE_LINES = 2500


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _page_entry_files() -> list[Path]:
    pages = AP_WEB / "pages"
    return sorted(p for p in pages.glob("*.tsx") if not p.name.endswith(".test.tsx"))


def _page_organism_files() -> list[Path]:
    org = AP_WEB / "pages" / "organisms"
    if not org.exists():
        return []
    return sorted(p for p in org.rglob("*.tsx") if not p.name.endswith(".test.tsx"))


def _shell_entry_files() -> list[Path]:
    shell = AP_WEB / "shell"
    return sorted(
        p
        for p in shell.glob("*.tsx")
        if not p.name.endswith(".test.tsx") and p.parent == shell
    )


def _component_files_excl_ai() -> list[Path]:
    comps = AP_WEB / "components"
    return sorted(
        p
        for p in comps.rglob("*.tsx")
        if not p.name.endswith(".test.tsx") and "ai-elements" not in p.parts
    )


def _ai_element_entry_files() -> list[Path]:
    ai = AP_WEB / "components" / "ai-elements"
    if not ai.exists():
        return []
    entries: list[Path] = []
    for d in sorted(ai.iterdir()):
        if not d.is_dir():
            continue
        stub = d / f"{d.name}.tsx"
        if stub.exists():
            entries.append(stub)
    return entries


def _python_modules() -> list[Path]:
    return sorted((REPO_ROOT / "omnigent").rglob("*.py"))


def _inline_component_count(paths: list[Path]) -> int:
    pattern = re.compile(r"^(function |const \w+ = \()", re.MULTILINE)
    return sum(len(pattern.findall(p.read_text(encoding="utf-8"))) for p in paths)


def test_page_entry_files_under_atomize_threshold() -> None:
    offenders = [
        (p.name, _line_count(p))
        for p in _page_entry_files()
        if _line_count(p) > MAX_PAGE_ENTRY_LINES
    ]
    assert offenders == [], f"Page entries exceed {MAX_PAGE_ENTRY_LINES}: {offenders}"


def test_page_organism_files_under_atomize_threshold() -> None:
    offenders = [
        (str(p.relative_to(REPO_ROOT)), _line_count(p))
        for p in _page_organism_files()
        if _line_count(p) > MAX_PAGE_ORGANISM_LINES
    ]
    assert offenders == [], f"Page organisms exceed {MAX_PAGE_ORGANISM_LINES}: {offenders}"


def test_shell_entry_files_under_atomize_threshold() -> None:
    offenders = [
        (p.name, _line_count(p))
        for p in _shell_entry_files()
        if _line_count(p) > MAX_SHELL_ENTRY_LINES
    ]
    assert offenders == [], f"Shell entries exceed {MAX_SHELL_ENTRY_LINES}: {offenders}"


def test_component_files_under_atomize_threshold() -> None:
    offenders = [
        (str(p.relative_to(REPO_ROOT)), _line_count(p))
        for p in _component_files_excl_ai()
        if _line_count(p) > MAX_COMPONENT_LINES
    ]
    assert offenders == [], f"Components exceed {MAX_COMPONENT_LINES}: {offenders}"


def test_ai_element_entry_files_under_atomize_threshold() -> None:
    offenders = [
        (p.name, _line_count(p))
        for p in _ai_element_entry_files()
        if _line_count(p) > MAX_AI_ELEMENT_ENTRY_LINES
    ]
    assert offenders == [], f"ai-elements entries exceed {MAX_AI_ELEMENT_ENTRY_LINES}: {offenders}"


def test_no_inline_components_in_page_entries() -> None:
    assert _inline_component_count(_page_entry_files()) == 0


def test_python_modules_under_atomize_threshold() -> None:
    offenders = [
        (str(p.relative_to(REPO_ROOT)), _line_count(p))
        for p in _python_modules()
        if _line_count(p) > MAX_PYTHON_MODULE_LINES
    ]
    assert offenders == [], f"Python modules exceed {MAX_PYTHON_MODULE_LINES}: {offenders}"


def test_decomposed_python_facades_importable() -> None:
    """Import facades via project venv (uv) — matches production/dev workflow."""
    code = """
import omnigent
import bytedesk_omnigent
from omnigent.server.routes import sessions
from omnigent.runner import app as runner_app
from omnigent import cli
assert sessions.create_sessions_router is not None
assert runner_app.create_runner_app is not None
assert cli.cli is not None
print('imports ok')
"""
    proc = subprocess.run(
        ["uv", "run", "python", "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_atomize_survey_reports_clean() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "atomize_survey.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "zero high-priority targets" in proc.stdout