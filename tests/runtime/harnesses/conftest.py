"""
Conftest for the harness process-manager / runner tests.

Ensures the runner subprocesses these tests spawn can import both
the production ``omnigent`` package AND the test fixture harness
at ``tests.runtime.harnesses._test_harness``.

pytest's :data:`pyproject.toml` ``pythonpath = ["."]`` adds the
project root to ``sys.path`` of the test process — but
:func:`asyncio.create_subprocess_exec` only inherits the OS env
(``PYTHONPATH``), not the parent's ``sys.path`` mutations. Without
this fixture the runner subprocess starts with no project root on
its path and fails to import either ``omnigent.runtime.harnesses._runner``
or the test harness module.

The fixture is autouse-scoped to this directory, so every spawn
in these tests inherits a PYTHONPATH that includes the project
root. Setting it via :func:`monkeypatch.setenv` keeps the
modification scoped to one test — other test modules that don't
care about PYTHONPATH are unaffected.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

# Project root: three parents up from this conftest
# (tests/runtime/harnesses/conftest.py → repo root).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_TEST_HARNESS_NAME = "test"
_TEST_HARNESS_MODULE = "tests.runtime.harnesses._test_harness"


@pytest.fixture(autouse=True)
def _ensure_subprocess_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Prepend the project root to the ``PYTHONPATH`` env var for the
    duration of the test, so spawned subprocesses can import
    ``omnigent`` and ``tests.*``.

    Prepend (don't overwrite) so any developer-set ``PYTHONPATH``
    is preserved as the suffix.
    """
    existing = os.environ.get("PYTHONPATH", "")
    new_path = f"{_PROJECT_ROOT}{os.pathsep}{existing}" if existing else str(_PROJECT_ROOT)
    monkeypatch.setenv("PYTHONPATH", new_path)


@pytest.fixture
def register_test_harness() -> Iterator[None]:
    """Register the fixture harness for one test, then restore the registry."""
    _HARNESS_MODULES[_TEST_HARNESS_NAME] = _TEST_HARNESS_MODULE
    try:
        yield
    finally:
        _HARNESS_MODULES.pop(_TEST_HARNESS_NAME, None)


@pytest.fixture
def short_tmp_parent() -> Iterator[Path]:
    """Short writable parent dir for per-conversation Unix socket paths."""
    roots = [Path("/tmp")]
    temp_root = Path(tempfile.gettempdir())
    if temp_root not in roots:
        roots.append(temp_root)

    last_error: OSError | None = None
    for root in roots:
        parent = root / f"omni-pm-{uuid.uuid4().hex[:8]}"
        try:
            parent.mkdir(mode=0o700)
        except OSError as exc:
            last_error = exc
            continue
        try:
            yield parent
        finally:
            shutil.rmtree(parent, ignore_errors=True)
        return

    assert last_error is not None
    raise last_error


@pytest.fixture
def manager(
    short_tmp_parent: Path,
    register_test_harness: None,
) -> HarnessProcessManager:
    """Harness process manager rooted in an isolated tmp dir."""
    return HarnessProcessManager(
        idle_timeout_s=60.0,
        reaper_interval_s=60.0,
        tmp_parent=short_tmp_parent,
    )
