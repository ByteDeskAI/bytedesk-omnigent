"""Edge tests for default DB and artifact path helpers in :mod:`omnigent.cli`."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.cli import _default_artifact_location, _default_db_uri


def test_default_db_uri_uses_local_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "omnigent-data"
    monkeypatch.setattr("omnigent.host.local_server._local_data_dir", lambda: data_dir)
    assert _default_db_uri() == f"sqlite:///{data_dir / 'chat.db'}"


def test_default_artifact_location_uses_local_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "omnigent-data"
    monkeypatch.setattr("omnigent.host.local_server._local_data_dir", lambda: data_dir)
    assert _default_artifact_location() == str(data_dir / "artifacts")
