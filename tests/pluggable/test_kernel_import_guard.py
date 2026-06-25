"""Kernel import boundary guard (BDP-2504, Section 8.1).

The microkernel refactor (epic BDP-2503) locks an eight-file kernel list
(Section 8.1 of ``docs/EXTENSION_FRAMEWORK_ANALYSIS.md``). The load-bearing
invariant: **no kernel file imports a non-kernel ``omnigent.*`` module at module
scope.** That is what makes the kernel independently testable with zero domain
dependencies — domain imports are deferred into functions / ``_lifespan`` or
injected as parameters (the ``omnigent/server/lifespan_phases.py`` pattern).

Two complementary angles, both source-level so the noisy ``omnigent/__init__``
package init (which eagerly imports many domain modules — a pre-existing
condition, and ``__init__`` is *not* a kernel file) cannot mask a real leak:

1. :func:`test_kernel_file_has_no_nonkernel_module_scope_import` — parse each
   kernel file's AST and assert its **top-level** (module-scope) ``import`` /
   ``from ... import`` statements reference only kernel modules / stdlib /
   third-party, never another ``omnigent.*`` subpackage outside the kernel list.
   Imports nested inside functions (deferred imports) are intentionally ignored.

2. :func:`test_importing_kernel_module_adds_no_nonkernel_omnigent` — a runtime
   cross-check in a fresh subprocess: after the ``omnigent`` package init
   baseline, importing a kernel module must add only kernel ``omnigent.*``
   entries to ``sys.modules``.

``omnigent/server/app.py`` is the eighth kernel file but only its *composition
root fragment* is kernel — the 2000+ lines of first-party route mounting are
plugin contributions (Section 9), so app.py legitimately imports domain code and
is excluded from the module-scope-purity assertion here.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

import omnigent

_REPO_ROOT = pathlib.Path(omnigent.__file__).resolve().parent.parent

# Section 8.1 kernel files that must stay free of non-kernel module-scope
# omnigent imports. ``omnigent/server/app.py`` is intentionally excluded — only
# its composition-root fragment is kernel; the file as a whole mounts domain
# routes (first-party plugin contributions) and so imports domain code.
_KERNEL_PURE_FILES = (
    "omnigent/extensions.py",
    "omnigent/pluggable/__init__.py",
    "omnigent/pluggable/registry.py",
    "omnigent/pluggable/manifest.py",
    "omnigent/pluggable/errors.py",
    "omnigent/server/lifespan_phases.py",
    "omnigent/server/service_registry.py",
)

# The omnigent modules a kernel file MAY import at module scope: the kernel set
# itself plus the bare ``omnigent`` / ``omnigent.server`` package anchors.
_ALLOWED_KERNEL_MODULES = frozenset(
    {
        "omnigent",
        "omnigent.extensions",
        "omnigent.pluggable",
        "omnigent.pluggable.registry",
        "omnigent.pluggable.manifest",
        "omnigent.pluggable.errors",
        "omnigent.server",
        "omnigent.server.lifespan_phases",
        "omnigent.server.service_registry",
    }
)


def _module_scope_omnigent_imports(path: pathlib.Path) -> list[str]:
    """Return module-scope (top-level) ``omnigent.*`` imports in ``path``.

    Only inspects ``tree.body`` — statements at module scope. Imports nested in
    functions / methods (the deferred-import pattern the kernel relies on) are
    excluded by construction, which is exactly the boundary we guard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("omnigent"):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Absolute ``from omnigent.x import y``. Relative imports (level > 0)
            # within the kernel package resolve to kernel siblings; we resolve
            # them to their absolute form for the allow-list check.
            if node.level:
                base_parts = path.relative_to(_REPO_ROOT).with_suffix("").parts
                # drop the file name to get the package, then walk up ``level``-1.
                pkg = list(base_parts[:-1])
                for _ in range(node.level - 1):
                    if pkg:
                        pkg.pop()
                module = ".".join([*pkg, node.module]) if node.module else ".".join(pkg)
                if module.startswith("omnigent"):
                    found.append(module)
            elif node.module and node.module.startswith("omnigent"):
                found.append(node.module)
    return found


@pytest.mark.parametrize("rel_path", _KERNEL_PURE_FILES)
def test_kernel_file_has_no_nonkernel_module_scope_import(rel_path: str) -> None:
    """A kernel file must not import a non-kernel omnigent module at module scope."""
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"kernel file missing: {rel_path}"
    imports = _module_scope_omnigent_imports(path)
    leaks = sorted(m for m in imports if m not in _ALLOWED_KERNEL_MODULES)
    assert not leaks, (
        f"{rel_path} imports non-kernel omnigent module(s) at module scope: "
        f"{leaks}. Defer these inside a function (the lifespan_phases.py pattern) "
        f"or inject them — the kernel must stay domain-free (Section 8.1)."
    )


@pytest.mark.parametrize(
    "module",
    [
        "omnigent.extensions",
        "omnigent.pluggable",
        "omnigent.pluggable.registry",
        "omnigent.pluggable.manifest",
        "omnigent.pluggable.errors",
        "omnigent.server.lifespan_phases",
        "omnigent.server.service_registry",
    ],
)
def test_importing_kernel_module_adds_no_nonkernel_omnigent(module: str) -> None:
    """Importing a kernel module adds only kernel ``omnigent.*`` to sys.modules.

    Runtime cross-check of the AST guard. Baseline is ``import omnigent`` (the
    package init, whose eager domain imports are pre-existing and not a kernel
    file). The delta a kernel module adds beyond that baseline must contain no
    non-kernel ``omnigent.*`` entry. Runs in a fresh subprocess so an unrelated
    test can't pre-import a domain module and mask a leak.
    """
    allowed = sorted(_ALLOWED_KERNEL_MODULES)
    probe = (
        "import sys, importlib\n"
        "importlib.import_module('omnigent')\n"
        "base = {n for n in sys.modules if n.startswith('omnigent')}\n"
        f"importlib.import_module({module!r})\n"
        "after = {n for n in sys.modules if n.startswith('omnigent')}\n"
        f"allowed = set({allowed!r})\n"
        "leak = sorted(m for m in (after - base) if m not in allowed)\n"
        "assert not leak, "
        f"'importing {module} pulled non-kernel omnigent modules: ' + repr(leak)\n"
    )
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"kernel import boundary violated by {module}.\nstdout:\n{result.stdout}"
        f"\nstderr:\n{result.stderr}"
    )
