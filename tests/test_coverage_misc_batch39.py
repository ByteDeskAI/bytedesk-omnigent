"""Batch-39 coverage for /compact unsupported, /context errors, overview fallbacks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omnigent_ui_sdk import RichBlockFormatter

from omnigent.repl._repl import (
    _collect_overview_targets,
    _update_context_ring_estimate,
    handle_slash_command,
)
from tests.repl.helpers import CapturingHost


class _RingHost:
    def __init__(self) -> None:
        self.ring_updates: list[tuple[int, int]] = []

    def update_context_usage(self, tokens: int, context_window: int) -> None:
        self.ring_updates.append((tokens, context_window))


@dataclass
class _OverviewSession:
    model: str = "test-agent"
    session_id: str | None = None
    current_response_id: str | None = None
    llm_model: str | None = None
    context_window: int = 200_000
    is_streaming: bool = False
    model_override: str | None = None


@pytest.mark.asyncio
async def test_compact_reports_unsupported_when_session_has_no_hook() -> None:
    host = CapturingHost()
    session = _OverviewSession()

    await handle_slash_command(
        "/compact",
        session,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )

    assert "does not support /compact" in host.text


@pytest.mark.asyncio
async def test_context_reports_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _failing_fetch(*_args: object, **_kwargs: object) -> Any:
        from omnigent.repl._repl import _ContextItems

        return _ContextItems(items=[], error="history unavailable")

    monkeypatch.setattr("omnigent.repl._repl._fetch_context_items", _failing_fetch)

    host = CapturingHost()
    session = _OverviewSession(session_id="conv_ctx", context_window=100_000)

    await handle_slash_command(
        "/context",
        session,  # type: ignore[arg-type]
        MagicMock(),
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )

    assert "Error fetching history: history unavailable" in host.text


@pytest.mark.asyncio
async def test_update_context_ring_estimate_noops_on_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _failing_fetch(*_args: object, **_kwargs: object) -> Any:
        from omnigent.repl._repl import _ContextItems

        return _ContextItems(items=[], error="items down")

    monkeypatch.setattr("omnigent.repl._repl._fetch_context_items", _failing_fetch)

    host = _RingHost()
    await _update_context_ring_estimate(
        _OverviewSession(session_id="conv_ring"),  # type: ignore[arg-type]
        MagicMock(),
        host,  # type: ignore[arg-type]
        200_000,
    )
    assert host.ring_updates == []


@pytest.mark.asyncio
async def test_collect_overview_targets_legacy_without_response_id() -> None:
    session = _OverviewSession(session_id=None, current_response_id=None)
    targets = await _collect_overview_targets(MagicMock(), session)  # type: ignore[arg-type]

    assert len(targets) == 1
    assert targets[0].key == "main"


@pytest.mark.asyncio
async def test_collect_overview_targets_legacy_without_conversation_id() -> None:
    client = MagicMock()
    client.responses.get = AsyncMock(return_value=MagicMock(conversation=None))

    session = _OverviewSession(session_id=None, current_response_id="resp_no_conv")
    targets = await _collect_overview_targets(client, session)  # type: ignore[arg-type]

    assert len(targets) == 1
    assert targets[0].key == "main"


@pytest.mark.asyncio
async def test_collect_overview_targets_returns_main_when_items_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.repl._repl._list_all_conversation_items",
        AsyncMock(side_effect=RuntimeError("items failed")),
    )

    session = _OverviewSession(session_id="conv_items_fail")
    targets = await _collect_overview_targets(MagicMock(), session)  # type: ignore[arg-type]

    assert len(targets) == 1
    assert targets[0].key == "conv_items_fail"