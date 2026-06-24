"""Edge-path coverage for :mod:`omnigent.runner.identity`."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.identity import (
    RUNNER_ID_ENV_VAR,
    _default_runner_id_path,
    get_stable_runner_id,
    load_or_create_runner_id,
)


def test_default_runner_id_path_points_at_omnigent_cache() -> None:
    assert _default_runner_id_path() == Path.home() / ".omnigent" / "runners" / "runner_id"


def test_get_stable_runner_id_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RUNNER_ID_ENV_VAR, "  runner_from_env  ")
    assert get_stable_runner_id() == "runner_from_env"


def test_get_stable_runner_id_rejects_empty_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RUNNER_ID_ENV_VAR, "   ")
    with pytest.raises(RuntimeError, match="must not be empty"):
        get_stable_runner_id()


def test_get_stable_runner_id_loads_or_creates_cache_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "runner_id"
    monkeypatch.delenv(RUNNER_ID_ENV_VAR, raising=False)
    monkeypatch.setattr("omnigent.runner.identity._default_runner_id_path", lambda: cache)

    first = get_stable_runner_id()
    second = get_stable_runner_id()

    assert first.startswith("runner_")
    assert first == second
    assert cache.read_text().strip() == first


def test_load_or_create_runner_id_reads_existing_file(tmp_path) -> None:
    path = tmp_path / "runner_id"
    path.write_text("runner_cached\n")
    assert load_or_create_runner_id(path) == "runner_cached"


def test_load_or_create_runner_id_creates_nested_cache(tmp_path) -> None:
    path = tmp_path / "nested" / "runner_id"
    runner_id = load_or_create_runner_id(path)
    assert runner_id.startswith("runner_")
    assert path.read_text().strip() == runner_id


def test_load_or_create_runner_id_rejects_empty_cache_file(tmp_path) -> None:
    path = tmp_path / "runner_id"
    path.write_text("")
    with pytest.raises(RuntimeError, match="runner id file is empty"):
        load_or_create_runner_id(path)