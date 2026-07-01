#!/usr/bin/env python3
"""Global atomize survey — adapted from bytedesk-atomize SKILL.md Step 1 for ap-web + omnigent."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AP = ROOT / "ap-web" / "src"

# Priority thresholds (adapted for Vite ap-web + Python packages).
P1_PAGE_ENTRY = 150
P1_PAGE_ORGANISM = 250
P1_SHELL_ENTRY = 300
P1_COMPONENT = 500
P1_AI_ELEMENT_ENTRY = 250
P1_PYTHON = 2500


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def tsx_files(base: Path, *, exclude_test: bool = True) -> list[Path]:
    out: list[Path] = []
    for p in base.rglob("*.tsx"):
        if exclude_test and p.name.endswith(".test.tsx"):
            continue
        out.append(p)
    return sorted(out)


def py_files(base: Path) -> list[Path]:
    return sorted(base.rglob("*.py"))


def inline_components(paths: list[Path]) -> list[tuple[str, int]]:
    pat = re.compile(r"^(function |const \w+ = \()", re.MULTILINE)
    hits: list[tuple[str, int]] = []
    for p in paths:
        n = len(pat.findall(p.read_text(encoding="utf-8")))
        if n:
            hits.append((str(p.relative_to(ROOT)), n))
    return hits


def survey() -> dict[str, list[tuple[str, int]]]:
    page_entries = [p for p in (AP / "pages").glob("*.tsx") if not p.name.endswith(".test.tsx")]
    page_organisms = tsx_files(AP / "pages" / "organisms") if (AP / "pages" / "organisms").exists() else []
    shell_entries = [
        p for p in (AP / "shell").glob("*.tsx") if not p.name.endswith(".test.tsx")
    ]
    components = [
        p
        for p in tsx_files(AP / "components")
        if "ai-elements" not in p.parts
    ]
    ai_entries: list[tuple[str, int]] = []
    ai_root = AP / "components" / "ai-elements"
    if ai_root.exists():
        for d in sorted(ai_root.iterdir()):
            if not d.is_dir():
                continue
            stub = d / f"{d.name}.tsx"
            if stub.exists():
                ai_entries.append((str(stub.relative_to(ROOT)), line_count(stub)))
    python = py_files(ROOT / "omnigent")

    def offenders(paths: list[Path], limit: int) -> list[tuple[str, int]]:
        return sorted(
            [(str(p.relative_to(ROOT)), line_count(p)) for p in paths if line_count(p) > limit],
            key=lambda x: -x[1],
        )

    return {
        "page_entries": offenders(page_entries, P1_PAGE_ENTRY),
        "page_organisms": offenders(page_organisms, P1_PAGE_ORGANISM),
        "shell_entries": offenders(shell_entries, P1_SHELL_ENTRY),
        "components": offenders(components, P1_COMPONENT),
        "ai_elements": [(p, n) for p, n in ai_entries if n > P1_AI_ELEMENT_ENTRY],
        "inline_page_entries": inline_components(page_entries),
        "python": offenders(python, P1_PYTHON),
    }


def main() -> int:
    results = survey()
    total_p1 = sum(len(v) for k, v in results.items() if k != "inline_page_entries")
    inline = results["inline_page_entries"]

    print("## Atomize Global Survey")
    print()
    for key, items in results.items():
        if key == "inline_page_entries":
            continue
        print(f"### {key} (P1 offenders: {len(items)})")
        for path, n in items[:15]:
            print(f"  {n:5d}  {path}")
        if not items:
            print("  (none)")
        print()

    print(f"### inline_page_entries: {sum(n for _, n in inline)} in {len(inline)} files")
    for path, n in inline:
        print(f"  {n:5d}  {path}")
    print()

    if total_p1 == 0 and not inline:
        print(
            "SYNTHESIS: zero high-priority targets — the entire repository is "
            "componentized/atomized with no remaining extraction candidates above priority thresholds."
        )
        return 0

    print(
        f"SYNTHESIS: {total_p1} high-priority target(s) remain — "
        "continue atomize runs on listed paths."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())