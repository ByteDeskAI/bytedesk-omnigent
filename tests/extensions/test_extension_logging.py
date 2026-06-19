"""BDP-2301 — the extension surfaces its own namespace's INFO logs (mirrors core's
post-dictConfig omnigent-namespace level set), so e.g. the bridge-installed line shows."""

from __future__ import annotations

import asyncio
import logging

from bytedesk_omnigent.extension import BytedeskExtension


def test_configure_logging_sets_bytedesk_namespace_to_info(monkeypatch):
    monkeypatch.delenv("OMNIGENT_LOG_LEVEL", raising=False)
    logging.getLogger("bytedesk_omnigent").setLevel(logging.WARNING)  # non-INFO baseline
    asyncio.run(BytedeskExtension()._configure_logging())
    assert logging.getLogger("bytedesk_omnigent").level == logging.INFO


def test_configure_logging_honors_omnigent_log_level(monkeypatch):
    monkeypatch.setenv("OMNIGENT_LOG_LEVEL", "DEBUG")
    asyncio.run(BytedeskExtension()._configure_logging())
    assert logging.getLogger("bytedesk_omnigent").level == logging.DEBUG


def test_configure_logging_runs_first_so_later_tasks_log(monkeypatch):
    names = [t.__name__ for t in BytedeskExtension().background_tasks()]
    assert names[0] == "_configure_logging"
