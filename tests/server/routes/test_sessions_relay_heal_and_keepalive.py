"""Runner availability: relay-ready heal-and-retry + viewer keepalive (BDP-2601).

Two hardening surfaces:

* ``_ensure_runner_relay_ready_with_heal`` — the message-send / relay-ready
  path now self-heals a dead/ephemeral runner exactly like
  ``_proxy_with_runner_heal`` (BDP-2579): on ``RUNNER_UNAVAILABLE`` with a
  FastAPI request available, relaunch + re-resolve conv/client + retry ONCE;
  surface a clean ``RUNNER_UNAVAILABLE`` when the heal exhausts its rungs.
  Internal callers (``request is None``) keep one-shot behavior.

* ``_runner_keepalive_*`` — while a user holds a per-session SSE stream open,
  a keepalive pings the runner (a unary control-plane request the runner
  counts as activity) so the focused conversation's runner is not idle-reaped
  out from under the viewer. Bounded to attached viewers (refcounted), never
  the persistent server→runner relay (which would keep every runner warm).
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import sessions as S

pytestmark = pytest.mark.asyncio


def _conv(runner_id: str | None = "runner_dead") -> Conversation:
    return Conversation(
        id="conv1",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv1",
        agent_id="ag_test",
        runner_id=runner_id,
        host_id="host_a",
    )


class _Store:
    """Conversation store whose runner_id flips after a heal repins it."""

    def __init__(self, runner_id: str) -> None:
        self.runner_id = runner_id

    def get_conversation(self, _session_id: str) -> Conversation:
        return _conv(self.runner_id)


# ── Fix #1: relay-ready heal-and-retry ──────────────────────────────────


async def test_relay_ready_with_heal_heals_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"relay": 0, "heal": 0, "get_client": 0}

    async def _fake_relay(
        _sid: str, _rid: str | None, _client: object, _store: object | None = None
    ) -> None:
        calls["relay"] += 1
        if calls["relay"] == 1:
            raise OmnigentError("runner offline", code=ErrorCode.RUNNER_UNAVAILABLE)

    async def _fake_heal(_sid: str, _request: object) -> bool:
        calls["heal"] += 1
        return True

    async def _fake_get_client(_sid: str, _router: object) -> object:
        calls["get_client"] += 1
        return MagicMock()

    monkeypatch.setattr(S, "_ensure_runner_relay_ready", _fake_relay)
    monkeypatch.setattr(S, "_heal_session_runner", _fake_heal)
    monkeypatch.setattr(S, "_get_runner_client", _fake_get_client)

    store = _Store("runner_new")
    new_conv, new_client = await S._ensure_runner_relay_ready_with_heal(
        "conv1",
        MagicMock(),  # request present → heal allowed
        _conv("runner_dead"),
        MagicMock(),
        store,  # type: ignore[arg-type]
        None,
    )

    assert calls == {"relay": 2, "heal": 1, "get_client": 1}
    assert new_conv.runner_id == "runner_new"  # re-resolved to the repinned runner
    assert new_client is not None


async def test_relay_ready_with_heal_surfaces_unavailable_when_heal_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"relay": 0, "heal": 0}

    async def _fake_relay(
        _sid: str, _rid: str | None, _client: object, _store: object | None = None
    ) -> None:
        calls["relay"] += 1
        raise OmnigentError("runner offline", code=ErrorCode.RUNNER_UNAVAILABLE)

    async def _fake_heal(_sid: str, _request: object) -> bool:
        calls["heal"] += 1
        return False  # exhausted every rung; graceful reconnecting state set

    monkeypatch.setattr(S, "_ensure_runner_relay_ready", _fake_relay)
    monkeypatch.setattr(S, "_heal_session_runner", _fake_heal)

    with pytest.raises(OmnigentError) as exc_info:
        await S._ensure_runner_relay_ready_with_heal(
            "conv1",
            MagicMock(),
            _conv(),
            MagicMock(),
            _Store("runner_new"),  # type: ignore[arg-type]
            None,
        )

    assert exc_info.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert calls == {"relay": 1, "heal": 1}  # no retry after a failed heal


async def test_relay_ready_with_heal_unavailable_when_client_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heal reports success but no client re-resolves → clean RUNNER_UNAVAILABLE."""
    relay_calls = {"n": 0}

    async def _fake_relay(
        _sid: str, _rid: str | None, _client: object, _store: object | None = None
    ) -> None:
        relay_calls["n"] += 1
        raise OmnigentError("runner offline", code=ErrorCode.RUNNER_UNAVAILABLE)

    async def _fake_heal(_sid: str, _request: object) -> bool:
        return True

    async def _fake_get_client(_sid: str, _router: object) -> object | None:
        return None  # repin raced / no client yet

    monkeypatch.setattr(S, "_ensure_runner_relay_ready", _fake_relay)
    monkeypatch.setattr(S, "_heal_session_runner", _fake_heal)
    monkeypatch.setattr(S, "_get_runner_client", _fake_get_client)

    with pytest.raises(OmnigentError) as exc_info:
        await S._ensure_runner_relay_ready_with_heal(
            "conv1", MagicMock(), _conv(), MagicMock(), _Store("x"), None  # type: ignore[arg-type]
        )
    assert exc_info.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert relay_calls["n"] == 1  # never retried the handshake without a client


async def test_relay_ready_with_heal_internal_caller_does_not_heal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``request is None`` (internal caller) keeps today's one-shot behavior."""
    calls = {"relay": 0, "heal": 0}

    async def _fake_relay(
        _sid: str, _rid: str | None, _client: object, _store: object | None = None
    ) -> None:
        calls["relay"] += 1
        raise OmnigentError("runner offline", code=ErrorCode.RUNNER_UNAVAILABLE)

    async def _fake_heal(_sid: str, _request: object) -> bool:
        calls["heal"] += 1
        return True

    monkeypatch.setattr(S, "_ensure_runner_relay_ready", _fake_relay)
    monkeypatch.setattr(S, "_heal_session_runner", _fake_heal)

    with pytest.raises(OmnigentError) as exc_info:
        await S._ensure_runner_relay_ready_with_heal(
            "conv1",
            None,  # internal caller — no request to drive a heal
            _conv(),
            MagicMock(),
            _Store("runner_new"),  # type: ignore[arg-type]
            None,
        )

    assert exc_info.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert calls == {"relay": 1, "heal": 0}  # never attempted to heal


async def test_relay_ready_with_heal_passes_through_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"relay": 0, "heal": 0}

    async def _fake_relay(
        _sid: str, _rid: str | None, _client: object, _store: object | None = None
    ) -> None:
        calls["relay"] += 1

    async def _fake_heal(_sid: str, _request: object) -> bool:
        calls["heal"] += 1
        return True

    monkeypatch.setattr(S, "_ensure_runner_relay_ready", _fake_relay)
    monkeypatch.setattr(S, "_heal_session_runner", _fake_heal)

    conv0 = _conv("runner_live")
    client0 = MagicMock()
    out_conv, out_client = await S._ensure_runner_relay_ready_with_heal(
        "conv1", MagicMock(), conv0, client0, _Store("x"), None  # type: ignore[arg-type]
    )

    assert (out_conv, out_client) == (conv0, client0)  # unchanged on first-try success
    assert calls == {"relay": 1, "heal": 0}


async def test_relay_ready_with_heal_non_runner_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-RUNNER_UNAVAILABLE OmnigentError is re-raised without healing."""

    async def _fake_relay(
        _sid: str, _rid: str | None, _client: object, _store: object | None = None
    ) -> None:
        raise OmnigentError("bad input", code=ErrorCode.INVALID_INPUT)

    async def _fake_heal(_sid: str, _request: object) -> bool:
        raise AssertionError("heal must not run for a non-runner error")

    monkeypatch.setattr(S, "_ensure_runner_relay_ready", _fake_relay)
    monkeypatch.setattr(S, "_heal_session_runner", _fake_heal)

    with pytest.raises(OmnigentError) as exc_info:
        await S._ensure_runner_relay_ready_with_heal(
            "conv1", MagicMock(), _conv(), MagicMock(), _Store("x"), None  # type: ignore[arg-type]
        )
    assert exc_info.value.code == ErrorCode.INVALID_INPUT


# ── Fix #2: viewer-stream keepalive ─────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_keepalive() -> object:
    S._runner_keepalive_tasks.clear()
    yield
    for handle in list(S._runner_keepalive_tasks.values()):
        handle.task.cancel()
    S._runner_keepalive_tasks.clear()


async def test_keepalive_acquire_refcounts_and_release_cancels() -> None:
    S._acquire_runner_keepalive("conv1", None)
    S._acquire_runner_keepalive("conv1", None)  # second viewer — same task
    handle = S._runner_keepalive_tasks["conv1"]
    assert handle.refcount == 2
    assert not handle.task.done()

    S._release_runner_keepalive("conv1")  # one viewer leaves
    assert "conv1" in S._runner_keepalive_tasks
    assert S._runner_keepalive_tasks["conv1"].refcount == 1
    assert not handle.task.done()

    S._release_runner_keepalive("conv1")  # last viewer leaves → cancel
    assert "conv1" not in S._runner_keepalive_tasks
    with contextlib.suppress(asyncio.CancelledError):
        await handle.task
    assert handle.task.cancelled()


async def test_keepalive_release_unknown_session_is_noop() -> None:
    S._release_runner_keepalive("never_acquired")  # must not raise


async def test_keepalive_loop_pings_runner_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pings: list[str] = []

    class _Client:
        async def get(self, url: str, *, timeout: float | None = None) -> httpx.Response:
            del timeout
            pings.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url))

    async def _fake_get_client(_sid: str, _router: object) -> object:
        return _Client()

    monkeypatch.setattr(S, "_get_runner_client", _fake_get_client)

    task = asyncio.create_task(
        S._runner_keepalive_loop("conv1", None, interval_s=0.01)
    )
    # Let the loop tick a few times, then stop it.
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert pings, "keepalive loop should have pinged the runner at least once"
    assert all(url == "/health" for url in pings)


async def test_keepalive_loop_tolerates_dead_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ping (no client / transport error) is swallowed, loop survives."""
    attempts = {"n": 0}

    class _DeadClient:
        async def get(self, url: str, *, timeout: float | None = None) -> httpx.Response:
            del url, timeout
            raise httpx.ConnectError("runner gone")

    async def _fake_get_client(_sid: str, _router: object) -> object | None:
        attempts["n"] += 1
        return None if attempts["n"] == 1 else _DeadClient()

    monkeypatch.setattr(S, "_get_runner_client", _fake_get_client)

    task = asyncio.create_task(
        S._runner_keepalive_loop("conv1", None, interval_s=0.01)
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # survived a None client AND a ConnectError
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
