"""
Unit tests for the ``omnigent.runtime.sentry`` error/performance telemetry hook.

The scrubbing functions and option resolution are pure (no ``sentry_sdk``
import), so they are exercised directly without the SDK installed. ``init_sentry``
is tested only for its DSN-gated no-op path — the SDK ``init`` itself is not
re-tested (that is sentry-sdk's responsibility).
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from omnigent.runtime import sentry


class _FakeSentry:
    """Stand-in for the ``sentry_sdk`` module so the global-handler helpers are
    testable without the real SDK installed."""

    def __init__(self) -> None:
        self.captured: list[BaseException | None] = []
        self.flushed = 0

    def capture_exception(self, exc: BaseException | None = None) -> None:
        self.captured.append(exc)

    def flush(self, timeout: float | None = None) -> None:
        self.flushed += 1


@pytest.fixture(autouse=True)
def _clear_sentry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip Sentry env vars so tests are deterministic regardless of host env."""
    for key in (
        "OMNIGENT_SENTRY_DSN",
        "OMNIGENT_SENTRY_ENVIRONMENT",
        "SENTRY_ENVIRONMENT",
        "OMNIGENT_SENTRY_RELEASE",
        "SENTRY_RELEASE",
        "OMNIGENT_SENTRY_TRACES_SAMPLE_RATE",
        "OMNIGENT_OTEL_CAPTURE_CONTENT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_init_sentry_is_noop_when_dsn_unset() -> None:
    assert sentry.init_sentry("server") is False


def test_init_sentry_is_noop_when_dsn_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_SENTRY_DSN", "   ")
    assert sentry.init_sentry("host") is False


def test_resolve_options_scrubs_content_by_default() -> None:
    opts = sentry.resolve_options("http://k@obs/proj")

    assert opts["dsn"] == "http://k@obs/proj"
    assert opts["send_default_pii"] is False
    # Stack-frame locals are the dominant prompt/response leak in an agent
    # runtime — they MUST be off when scrubbing.
    assert opts["include_local_variables"] is False
    assert opts["max_request_body_size"] == "never"
    assert opts["before_send"] is sentry._scrub_event
    assert opts["before_send_transaction"] is sentry._scrub_transaction


def test_resolve_options_keeps_content_when_capture_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_OTEL_CAPTURE_CONTENT", "true")
    opts = sentry.resolve_options("http://k@obs/proj")

    assert opts["include_local_variables"] is True
    assert opts["max_request_body_size"] == "always"
    assert opts["before_send"] is None
    assert opts["before_send_transaction"] is None


def test_resolve_options_reads_environment_release_and_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_SENTRY_ENVIRONMENT", "production")
    monkeypatch.setenv("OMNIGENT_SENTRY_RELEASE", "0.13.0")
    monkeypatch.setenv("OMNIGENT_SENTRY_TRACES_SAMPLE_RATE", "0.25")
    opts = sentry.resolve_options("http://k@obs/proj")

    assert opts["environment"] == "production"
    assert opts["release"] == "0.13.0"
    assert opts["traces_sample_rate"] == 0.25


def test_resolve_options_defaults_rate_when_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_SENTRY_TRACES_SAMPLE_RATE", "not-a-number")
    opts = sentry.resolve_options("http://k@obs/proj")

    assert opts["traces_sample_rate"] == sentry._DEFAULT_TRACES_SAMPLE_RATE


def test_scrub_event_strips_request_body_keeps_exception_and_route() -> None:
    event = {
        "transaction": "POST /v1/sessions",
        "exception": {"values": [{"type": "ValueError", "value": "boom"}]},
        "tags": {"omnigent.component": "server"},
        "request": {
            "method": "POST",
            "url": "http://obs/v1/sessions",
            "data": {"prompt": "secret user message"},
            "query_string": "q=secret",
            "cookies": {"session": "abc"},
        },
    }

    scrubbed = sentry._scrub_event(event, {})

    assert "data" not in scrubbed["request"]
    assert "query_string" not in scrubbed["request"]
    assert "cookies" not in scrubbed["request"]
    # Route + error metadata survive.
    assert scrubbed["request"]["method"] == "POST"
    assert scrubbed["transaction"] == "POST /v1/sessions"
    assert scrubbed["exception"]["values"][0]["type"] == "ValueError"
    assert scrubbed["tags"]["omnigent.component"] == "server"


def test_scrub_event_tolerates_missing_request() -> None:
    event = {"exception": {"values": []}}
    assert sentry._scrub_event(event, {}) is event


def test_scrub_transaction_strips_span_data_keeps_op_and_description() -> None:
    event = {
        "type": "transaction",
        "transaction": "POST /v1/sessions",
        "request": {"data": {"prompt": "secret"}},
        "spans": [
            {
                "op": "http.client",
                "description": "POST openai",
                "data": {"gen_ai.request.messages": "secret prompt"},
            },
            {"op": "db.query", "description": "SELECT 1"},
        ],
    }

    scrubbed = sentry._scrub_transaction(event, {})

    assert "data" not in scrubbed["spans"][0]
    assert scrubbed["spans"][0]["op"] == "http.client"
    assert scrubbed["spans"][0]["description"] == "POST openai"
    assert scrubbed["spans"][1]["op"] == "db.query"
    assert "data" not in scrubbed["request"]


def test_scrub_transaction_tolerates_missing_spans() -> None:
    event = {"type": "transaction", "transaction": "GET /healthz"}
    assert sentry._scrub_transaction(event, {}) is event


def test_is_enabled_reflects_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sentry, "_enabled", False)
    assert sentry.is_enabled() is False
    monkeypatch.setattr(sentry, "_enabled", True)
    assert sentry.is_enabled() is True


def test_capture_and_flush_is_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSentry()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(sentry, "_enabled", False)

    sentry.capture_and_flush(ValueError("x"))

    assert fake.captured == []
    assert fake.flushed == 0


def test_capture_and_flush_captures_and_flushes_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSentry()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(sentry, "_enabled", True)
    boom = ValueError("boom")

    sentry.capture_and_flush(boom)

    assert fake.captured == [boom]
    assert fake.flushed == 1


def test_install_asyncio_handler_is_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSentry()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(sentry, "_enabled", False)

    async def _run() -> bool:
        loop = asyncio.get_running_loop()
        sentinel = lambda _loop, _ctx: None  # noqa: E731
        loop.set_exception_handler(sentinel)
        sentry.install_asyncio_exception_handler()
        # Disabled → handler not replaced.
        return loop.get_exception_handler() is sentinel

    assert asyncio.run(_run()) is True


def test_install_asyncio_handler_reports_and_chains_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSentry()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(sentry, "_enabled", True)
    boom = ValueError("loop boom")

    async def _run() -> list[dict]:
        loop = asyncio.get_running_loop()
        chained: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: chained.append(ctx))
        sentry.install_asyncio_exception_handler()
        loop.call_exception_handler({"message": "task failed", "exception": boom})
        return chained

    chained = asyncio.run(_run())

    # Reported to Sentry AND chained to the previously-installed handler.
    assert fake.captured == [boom]
    assert len(chained) == 1
