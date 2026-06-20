"""Import-safety guard for the seam modules behind the manifest (BDP-2374).

The just-fixed BDP-2371 regression was a seam module calling
``discover_extensions()`` at *import*, which loads the FastAPI-heavy entry-point
extensions onto the runner hot path. Discovery is now a server-startup concern
(:func:`omnigent.pluggable.manifest.discover_all_extensions`). These tests pin
that invariant from two angles:

1. The runner hot path stays FastAPI-free — the canonical net is
   ``tests/runner/test_identity.py``; here we re-assert it for the seam modules
   that sit closest to that path.
2. Importing any seam module must NOT *invoke* extension discovery — a freshly
   imported seam registry exposes only its built-in providers, never an
   extension-contributed one.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

# Seam modules that are on / near the runner hot path and must import without
# dragging in the FastAPI stack. (``omnigent.tools.builtins.web_search`` is
# deliberately excluded: it imports ``omnigent.tools.base``, which transitively
# imports FastAPI for unrelated reasons — that predates this work and is not the
# discovery regression these tests guard.)
_FASTAPI_FREE_SEAM_MODULES = (
    "omnigent.runtime.harnesses.descriptors",
    "omnigent.stores.factory",
    "omnigent.stores.memory_store.provider",
    "omnigent.spec.source",
    "omnigent.pluggable.registry",
)


@pytest.mark.parametrize("module", _FASTAPI_FREE_SEAM_MODULES)
def test_importing_seam_module_does_not_pull_in_fastapi(module: str) -> None:
    """Importing a seam module keeps the FastAPI stack out of sys.modules.

    A seam module that called ``discover_extensions()`` at import would drag the
    FastAPI-heavy extension hub onto the runner hot path. Runs in a fresh
    subprocess so an unrelated test can't pre-import FastAPI and mask it.
    """
    probe = (
        "import sys\n"
        f"import {module}\n"
        "assert 'fastapi' not in sys.modules, "
        f"'fastapi loaded via {module} import'\n"
    )
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"importing {module} pulled in the FastAPI stack "
        f"(a seam module discovered extensions at import). stderr:\n{result.stderr}"
    )


def test_building_seam_registries_does_not_invoke_discovery() -> None:
    """Building a seam's :class:`PluggableRegistry` must not *invoke* discovery.

    The contract is "no ``discover_extensions()`` at import/registry-build"
    (BDP-2371): discovery is server-startup-only, run once via
    :func:`omnigent.pluggable.manifest.discover_all_extensions`. We assert it
    directly on the registry-construction path the manifest accessors use — patch
    the hub's ``discover_extensions`` to record any call, build every seam's
    registry, and require zero calls.

    (This targets registry construction specifically, not blanket module import:
    importing ``omnigent.tools.builtins`` triggers the *tool-factory* extension
    merge in that package's ``__init__`` — a separate, pre-existing ADR-0143 path
    that is not the pluggable-seam regression guarded here.)

    We patch ``omnigent.pluggable.registry.discover_extensions`` — the proxy that
    ONLY ``PluggableRegistry.discover_extensions`` consults — so the assertion is
    isolated to the seam-discovery path and is not confused by the separate
    tool-factory extension merge that ``omnigent.tools.builtins`` runs at import.

    Run in a subprocess so the patch sees a clean import.
    """
    probe = (
        "import omnigent.pluggable.registry as reg\n"
        "calls = []\n"
        "reg.discover_extensions = lambda *a, **k: calls.append(1) or []\n"
        "from omnigent.pluggable.manifest import SEAMS\n"
        "for seam, accessor, _hook in SEAMS:\n"
        "    accessor()  # build the registry — must not discover\n"
        "assert calls == [], "
        "f'building a seam registry invoked seam discover_extensions(): {len(calls)} call(s)'\n"
        "# And discover_all_extensions DOES drive one discovery per seam:\n"
        "from omnigent.pluggable.manifest import discover_all_extensions\n"
        "discover_all_extensions()\n"
        "assert len(calls) == len(SEAMS), "
        "f'discover_all_extensions ran {len(calls)} of {len(SEAMS)} seam discoveries'\n"
    )
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"building a seam registry invoked extension discovery. stderr:\n{result.stderr}"
    )
