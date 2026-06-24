"""Failure-path coverage for claude-native helpers in :mod:`omnigent.runner.app`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent import claude_native_bridge
from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner.app import _auto_create_claude_terminal
from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
)


def _claude_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        AsyncMock(),
    )


class _CaptureRegistry:
    def __init__(self) -> None:
        self.specs: list[Any] = []
        self.terminal_registry = None

    async def launch_required_terminal(self, **kwargs: Any) -> SessionResourceView:
        self.specs.append(kwargs["spec"])
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=kwargs["session_id"],
            name="claude:main",
            metadata={"terminal_name": "claude", "session_key": "main", "running": True},
        )


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_fetch_config_http_error_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()

    async def _boom(url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        raise httpx.ConnectError("ap down")

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={})),
    )
    client.get = AsyncMock(side_effect=_boom)  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            "conv_claude_get_fail",
            registry,  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=client,
        )

    assert registry.specs, "terminal should still launch after config fetch failure"
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_reads_model_override_and_launch_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={
                    "model_override": "claude-sonnet-4-6",
                    "terminal_launch_args": ["--print", "hi"],
                    "labels": {
                        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY: "src-ext-uuid",
                        FORK_CARRY_HISTORY_LABEL_KEY: "1",
                    },
                },
            )
        ),
    )

    await _auto_create_claude_terminal(
        "conv_claude_cfg",
        registry,  # type: ignore[arg-type]
        lambda *_a, **_k: None,
        server_client=client,
    )

    args = registry.specs[0].args
    assert "--print" in args
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_cold_resume_transcript_failure_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.claude_native as claude_native

    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={"external_session_id": "claude-session-abc"},
            )
        ),
    )
    monkeypatch.setattr(
        claude_native,
        "_ensure_local_claude_resume_transcript",
        AsyncMock(side_effect=RuntimeError("no items")),
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            "conv_claude_cold_fail",
            registry,  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=client,
        )

    assert "Could not synthesize Claude resume transcript" in caplog.text
    assert "--resume" not in registry.specs[0].args
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_fork_clone_failure_launches_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.claude_native as claude_native

    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={
                    "external_session_id": None,
                    "labels": {FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY: "source-ext"},
                },
            )
        ),
    )
    monkeypatch.setattr(
        claude_native,
        "_clone_claude_transcript",
        lambda **_k: (_ for _ in ()).throw(OSError("missing source")),
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            "conv_claude_fork_fail",
            registry,  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=client,
        )

    assert "Could not clone source transcript" in caplog.text
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_fork_clone_patch_failure_still_launches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.claude_native as claude_native

    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()
    transcript = tmp_path / "cloned.jsonl"
    transcript.write_text("{}\n")

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={
                    "external_session_id": None,
                    "labels": {FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY: "source-ext"},
                },
            )
        ),
    )
    monkeypatch.setattr(
        claude_native,
        "_clone_claude_transcript",
        lambda **_k: transcript,
    )

    async def _patch_fail(url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        raise httpx.ConnectError("ap down")

    client.patch = AsyncMock(side_effect=_patch_fail)  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            "conv_claude_fork_patch",
            registry,  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=client,
        )

    assert "Could not pre-set external_session_id" in caplog.text
    assert "--resume" in registry.specs[0].args
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_fork_rebuild_failure_launches_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.claude_native as claude_native

    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={
                    "external_session_id": None,
                    "labels": {FORK_CARRY_HISTORY_LABEL_KEY: "1"},
                },
            )
        ),
    )
    monkeypatch.setattr(
        claude_native,
        "_ensure_local_claude_resume_transcript",
        AsyncMock(side_effect=RuntimeError("items missing")),
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            "conv_claude_rebuild_fail",
            registry,  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=client,
        )

    assert "Could not build native transcript from items" in caplog.text
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_fork_rebuild_patch_failure_still_launches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.claude_native as claude_native

    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()
    transcript = tmp_path / "built.jsonl"
    transcript.write_text("{}\n")

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={
                    "external_session_id": None,
                    "labels": {FORK_CARRY_HISTORY_LABEL_KEY: "1"},
                },
            )
        ),
    )
    monkeypatch.setattr(
        claude_native,
        "_ensure_local_claude_resume_transcript",
        AsyncMock(return_value=transcript),
    )

    async def _patch_fail(url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        raise httpx.ConnectError("ap down")

    client.patch = AsyncMock(side_effect=_patch_fail)  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            "conv_claude_rebuild_patch",
            registry,  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=client,
        )

    assert "Could not pre-set external_session_id" in caplog.text
    assert "--resume" in registry.specs[0].args
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_bridge_id_patch_failure_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"labels": {}}),
        ),
    )

    async def _patch_fail(url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        raise httpx.ConnectError("ap down")

    client.patch = AsyncMock(side_effect=_patch_fail)  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            "conv_claude_bridge_patch",
            registry,  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=client,
        )

    assert "Could not reset bridge_id label" in caplog.text
    assert registry.specs
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_provider_config_failure_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _claude_env(tmp_path, monkeypatch)
    registry = _CaptureRegistry()

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"labels": {}}),
        ),
    )
    monkeypatch.setattr(
        "omnigent.claude_native.resolve_native_claude_config",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("no provider")),
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            "conv_claude_provider_fail",
            registry,  # type: ignore[arg-type]
            lambda *_a, **_k: None,
            server_client=client,
        )

    assert "FALLING BACK to Claude Code's own login" in caplog.text
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_non_dict_metadata_still_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal resource payloads with non-dict metadata still publish."""
    import omnigent.entities.session_resources as session_resources

    _claude_env(tmp_path, monkeypatch)
    published: list[dict[str, Any]] = []

    def _capture(_session_id: str, event: dict[str, Any]) -> None:
        del _session_id
        published.append(event)

    class _BadMetadataRegistry(_CaptureRegistry):
        async def launch_required_terminal(self, **kwargs: Any) -> SessionResourceView:
            del kwargs
            return SessionResourceView(
                id="terminal_bad_meta",
                type="terminal",
                session_id="conv_bad_meta",
                name="claude:main",
                metadata={"terminal_name": "claude", "running": True},
            )

    registry = _BadMetadataRegistry()
    monkeypatch.setattr(
        session_resources,
        "session_resource_view_to_dict",
        lambda _view: {
            "id": "terminal_bad_meta",
            "metadata": "not-a-dict",
        },
    )

    client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"labels": {}}),
        ),
    )

    await _auto_create_claude_terminal(
        "conv_bad_meta",
        registry,  # type: ignore[arg-type]
        _capture,
        server_client=client,
    )

    assert any(e.get("type") == "session.resource.created" for e in published)
    await client.aclose()