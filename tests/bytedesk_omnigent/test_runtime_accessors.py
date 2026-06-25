"""Lazy runtime accessors for ByteDesk durable stores (BDP-2296)."""

from __future__ import annotations

from bytedesk_omnigent import runtime


class _ConvStore:
    def __init__(self, location: str) -> None:
        self.storage_location = location


def test_runtime_accessors_cache_per_conversation_store_uri(
    monkeypatch,
    tmp_path,
) -> None:
    location = f"sqlite:///{tmp_path / 'runtime.db'}"
    conv = _ConvStore(location)
    monkeypatch.setattr("bytedesk_omnigent.runtime.get_conversation_store", lambda: conv)

    runtime._signal_bus_cache.clear()
    runtime._cron_scheduler_cache.clear()
    runtime._tool_step_store_cache.clear()
    runtime._session_state_store_cache.clear()

    bus_a = runtime.get_signal_bus()
    bus_b = runtime.get_signal_bus()
    assert bus_a is bus_b

    sched_a = runtime.get_cron_scheduler()
    sched_b = runtime.get_cron_scheduler()
    assert sched_a is sched_b

    steps_a = runtime.get_tool_step_store()
    steps_b = runtime.get_tool_step_store()
    assert steps_a is steps_b

    state_a = runtime.get_session_state_store()
    state_b = runtime.get_session_state_store()
    assert state_a is state_b
