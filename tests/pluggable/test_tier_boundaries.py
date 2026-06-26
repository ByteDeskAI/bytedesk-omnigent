"""Tier-boundary guard (BDP-2518).

Extends the kernel import-guard (``tests/pluggable/test_kernel_import_guard.py``)
from the kernel-only invariant up to the full **three-tier one-way rule** from
[`docs/TIER_ARCHITECTURE.md`](../../docs/TIER_ARCHITECTURE.md):

```
kernel  ←  core  ←  extensions     (arrows = "is imported by";
                                     lower tiers never import higher tiers)
```

The kernel guard already enforces *kernel never imports non-kernel core* at
module scope (Tier 1 purity). This module adds the next boundary down:

**CORE never imports an EXTENSION at module scope.** Core (``omnigent/**`` minus
``omnigent/kernel``) is the out-of-the-box functionality; extensions
(``bytedesk_omnigent`` + any entry-point-discovered package) are optional,
domain-bound integrations. Core must boot and pass its tests with **no extension
installed**, so a module-scope ``import bytedesk_omnigent`` in a core file would
invert the dependency arrow and break the hard-fork pluggable posture
(ADR-0143 / BDP-2371).

The allowance — and the reason this is an AST *module-scope* scan, not a string
grep — is that **deferred imports are legitimate**: core may pull an extension
symbol inside a function body / lazy factory (the ``lifespan_phases.py``
deferred-import pattern), because that code path only runs when the extension is
actually present. The historical example is ``sessions.py``'s
``memory_tool_intercept`` seam; this run that hard-import was replaced by the
generic ``tool_interceptors()`` prefix table, so today core carries **zero**
extension imports of either kind — and :func:`test_sessions_memory_intercept_is_not_module_scope`
pins that ``sessions.py`` never reintroduces one at module scope (a regression
would have to keep it deferred).

All scanning mirrors the existing guard: parse ``tree.body`` only, so imports
nested in functions are excluded by construction — that is exactly the boundary.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import omnigent

_OMNIGENT_PKG = pathlib.Path(omnigent.__file__).resolve().parent
_REPO_ROOT = _OMNIGENT_PKG.parent

# Extension top-level package names. ``bytedesk_omnigent`` is the first-party
# extension (pyproject ``[project.entry-points."omnigent.extensions"]``); any
# future out-of-tree ``extensions/*`` package would be added here. These are the
# names core must never import at module scope.
_EXTENSION_TOP_LEVEL = frozenset({"bytedesk_omnigent"})

# Subtrees under ``omnigent/`` that are NOT core for this boundary:
#   - ``kernel`` is Tier 1 (covered by the kernel guard, and it never imports
#     extensions either — that is the kernel guard's job).
#   - ``db/migrations`` are append-only Alembic revisions that legitimately
#     reference extension tables by name in their *docstrings* (the ext tables
#     are in-chain); they ship no runtime import boundary and are data, not core
#     library code.
_NON_CORE_PARTS = ("kernel",)


def _core_python_files() -> list[pathlib.Path]:
    """Every core ``.py`` file: ``omnigent/**`` minus the non-core subtrees."""
    files: list[pathlib.Path] = []
    for path in _OMNIGENT_PKG.rglob("*.py"):
        parts = path.relative_to(_OMNIGENT_PKG).parts
        if parts and parts[0] in _NON_CORE_PARTS:
            continue
        if "migrations" in parts:
            continue
        files.append(path)
    return files


def _module_scope_extension_imports(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return ``(lineno, module)`` for module-scope extension imports in ``path``.

    Only inspects ``tree.body`` — statements at module scope. Imports nested in
    functions / methods (the deferred-import pattern core relies on for
    optional-extension code paths) are excluded by construction, which is exactly
    the boundary we guard. String references to an extension module (e.g.
    ``module_path="bytedesk_omnigent.harnesses..."`` for lazy harness loading)
    are not ``import`` statements and so are correctly ignored.
    """
    found: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _EXTENSION_TOP_LEVEL:
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) inside the ``omnigent`` package can
            # never resolve to a top-level extension package, so only absolute
            # ``from bytedesk_omnigent... import ...`` can cross the boundary.
            if (
                node.level == 0
                and node.module
                and node.module.split(".")[0] in _EXTENSION_TOP_LEVEL
            ):
                found.append((node.lineno, node.module))
    return found


def test_core_files_exist() -> None:
    """Sanity: the core scan actually finds files (guards an empty glob).

    A boundary test that scans zero files is a silent false-green. Pin a healthy
    lower bound so a broken path constant fails loudly instead of passing.

    :returns: None.
    """
    files = _core_python_files()
    assert len(files) > 50, (
        f"core scan found only {len(files)} files under {_OMNIGENT_PKG}; "
        "the path constant or glob is wrong — refusing a silent false-green."
    )


def test_core_does_not_import_extension_at_module_scope() -> None:
    """No core module imports an extension package at MODULE SCOPE.

    The Tier 2 → Tier 3 boundary: core (``omnigent/**`` minus ``kernel``) must
    boot with no extension installed, so it may only reach an extension through a
    deferred (function-body) import. A module-scope ``import bytedesk_omnigent``
    inverts the dependency arrow (``docs/TIER_ARCHITECTURE.md`` §1).

    If this fails on a real violation, **defer the import** into the function
    that uses it (the ``lifespan_phases.py`` pattern) or route it through the
    extension seam — do not weaken this test.

    :returns: None.
    """
    leaks: list[str] = []
    for path in _core_python_files():
        for lineno, module in _module_scope_extension_imports(path):
            rel = path.relative_to(_REPO_ROOT)
            leaks.append(f"{rel}:{lineno}: import {module}")

    assert not leaks, (
        "core imports an extension at module scope (Tier 2 must not depend on "
        "Tier 3):\n  " + "\n  ".join(sorted(leaks)) + "\nDefer the import into "
        "the using function or route it through the extension seam — the kernel "
        "<- core <- extensions arrow is one-way (docs/TIER_ARCHITECTURE.md §1)."
    )


def test_sessions_memory_intercept_is_not_module_scope() -> None:
    """``sessions.py`` carries no module-scope extension import.

    The ``memory__*`` tool dispatch historically hard-imported
    ``bytedesk_omnigent.memory_tool_intercept`` in core; it now flows through the
    generic ``tool_interceptors()`` prefix table, so there is no extension import
    here at all. This pins that whatever ``sessions.py`` does for that seam, it
    stays off module scope — a reintroduced hard-import (e.g. a future
    ``from bytedesk_omnigent.memory_tool_intercept import ...``) must be deferred,
    never top-level.

    :returns: None.
    """
    sessions = _OMNIGENT_PKG / "server" / "routes" / "sessions.py"
    assert sessions.is_file(), f"expected core file missing: {sessions}"

    leaks = _module_scope_extension_imports(sessions)
    assert not leaks, (
        "sessions.py imports an extension at module scope: "
        f"{[f'line {ln}: {m}' for ln, m in leaks]}. The memory_tool_intercept "
        "seam must stay deferred (inside the dispatch function) or route through "
        "the generic tool_interceptors() table — never a top-level import."
    )


@pytest.mark.parametrize("rel_path", ["omnigent/server/routes/sessions.py"])
def test_known_deferred_extension_imports_stay_deferred(rel_path: str) -> None:
    """Any extension import that *does* exist in a core file is function-scoped.

    Cross-check of the module-scope scan from the other direction: walk the full
    AST (not just ``tree.body``) and assert that every ``import``/``from`` of an
    extension package has at least one ``FunctionDef`` / ``AsyncFunctionDef`` /
    lambda ancestor — i.e. it is deferred. This catches a class-body or
    ``if TYPE_CHECKING:``-but-actually-runtime extension import that a
    ``tree.body``-only scan might phrase differently.

    :param rel_path: core file to inspect, relative to the repo root.
    :returns: None.
    """
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"expected core file missing: {rel_path}"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    non_deferred: list[int] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._fn_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._fn_depth += 1
            self.generic_visit(node)
            self._fn_depth -= 1

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._fn_depth += 1
            self.generic_visit(node)
            self._fn_depth -= 1

        def _names(self, node: ast.AST) -> list[str]:
            if isinstance(node, ast.Import):
                return [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                return [node.module]
            return []

        def visit_Import(self, node: ast.Import) -> None:
            self._check(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            self._check(node)

        def _check(self, node: ast.AST) -> None:
            for name in self._names(node):
                if name.split(".")[0] in _EXTENSION_TOP_LEVEL and self._fn_depth == 0:
                    non_deferred.append(getattr(node, "lineno", -1))

    _Visitor().visit(tree)
    assert not non_deferred, (
        f"{rel_path} has a non-deferred (module/class-scope) extension import at "
        f"line(s) {sorted(non_deferred)}; move it inside the using function."
    )
