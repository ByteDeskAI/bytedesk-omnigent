"""Batch-43 coverage for session status publish sticky-failed guard."""

from __future__ import annotations

import pytest

from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import _publish_status


def test_publish_status_blocks_idle_after_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_mod._session_status_cache.clear()
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        sessions_mod.session_stream,
        "publish",
        lambda _sid, payload: published.append(payload),
    )

    _publish_status("conv_status", "failed")
    assert sessions_mod._session_status_cache["conv_status"] == "failed"
    assert published[-1]["status"] == "failed"

    _publish_status("conv_status", "idle")
    assert sessions_mod._session_status_cache["conv_status"] == "failed"
    assert published[-1]["status"] == "failed"

    _publish_status("conv_status", "running")
    assert sessions_mod._session_status_cache["conv_status"] == "running"
    assert published[-1]["status"] == "running"

    sessions_mod._session_status_cache.clear()