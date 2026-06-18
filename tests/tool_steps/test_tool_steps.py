"""Tests for the durable deterministic tool-step store: idempotent claim,
deterministic re-entry, retry/timeout-over-session, resume-on-restart
(BDP-2252 α5, ADR-0142, aligned ADR-0009)."""
from __future__ import annotations

import pytest

from omnigent.tool_steps import (
    SqlAlchemyToolStepStore,
    StepOutcome,
    ToolStepBusy,
    ToolStepExhausted,
    run_tool_step,
)


def _store(tmp_path) -> SqlAlchemyToolStepStore:
    # Mirrors tests/bus/test_signal_bus.py::_bus — SQLite on tmp_path.
    return SqlAlchemyToolStepStore(f"sqlite:///{tmp_path / 'steps.db'}")


def test_begin_claims_then_complete_then_replay_returns_cached_result(tmp_path) -> None:
    store = _store(tmp_path)

    first = store.begin(session_id="s1", step_key="k1", tool_name="t", now=100)
    assert first.outcome is StepOutcome.CLAIMED
    assert first.step.status == "running"
    assert first.step.attempts == 1

    assert store.complete(session_id="s1", step_key="k1", result={"ok": 1}, now=101)

    # A replay of the SAME step returns the cached result — no re-execution.
    replay = store.begin(session_id="s1", step_key="k1", tool_name="t", now=102)
    assert replay.outcome is StepOutcome.ALREADY_COMPLETED
    assert replay.step.result == {"ok": 1}


def test_complete_is_idempotent_only_first_owns_running_claim(tmp_path) -> None:
    store = _store(tmp_path)
    store.begin(session_id="s1", step_key="k1", tool_name="t", now=100)

    assert store.complete(session_id="s1", step_key="k1", result={"v": 1}, now=101) is True
    # A replayed completion of an already-completed step does not win the guard.
    assert store.complete(session_id="s1", step_key="k1", result={"v": 2}, now=102) is False
    assert store.get(session_id="s1", step_key="k1").result == {"v": 1}


def test_run_tool_step_executes_once_then_returns_cached_on_replay(tmp_path) -> None:
    store = _store(tmp_path)
    calls = {"n": 0}

    def run():
        calls["n"] += 1
        return {"answer": 42}

    first = run_tool_step(
        store, session_id="s1", step_key="k1", tool_name="t", run=run
    )
    second = run_tool_step(
        store, session_id="s1", step_key="k1", tool_name="t", run=run
    )

    assert first == {"answer": 42}
    assert second == {"answer": 42}
    # Deterministic re-entry: the side-effecting body ran exactly once.
    assert calls["n"] == 1


def test_run_tool_step_retries_to_cap_then_raises_exhausted(tmp_path) -> None:
    store = _store(tmp_path)
    calls = {"n": 0}

    def run():
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ToolStepExhausted):
        run_tool_step(
            store,
            session_id="s1",
            step_key="k1",
            tool_name="t",
            run=run,
            max_attempts=3,
        )

    # Tried exactly max_attempts times, then the step is terminally failed.
    assert calls["n"] == 3
    assert store.get(session_id="s1", step_key="k1").status == "failed"
    # A later begin sees the dead step (no further execution).
    assert (
        store.begin(session_id="s1", step_key="k1", tool_name="t").outcome
        is StepOutcome.EXHAUSTED
    )


def test_run_tool_step_succeeds_after_transient_failures(tmp_path) -> None:
    store = _store(tmp_path)
    calls = {"n": 0}

    def run():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return {"ok": True}

    result = run_tool_step(
        store,
        session_id="s1",
        step_key="k1",
        tool_name="t",
        run=run,
        max_attempts=3,
    )
    assert result == {"ok": True}
    assert calls["n"] == 2
    assert store.get(session_id="s1", step_key="k1").status == "completed"


def test_running_step_within_deadline_returns_running(tmp_path) -> None:
    store = _store(tmp_path)
    store.begin(
        session_id="s1",
        step_key="k1",
        tool_name="t",
        timeout_seconds=300,
        now=100,
    )
    # A concurrent claim within the deadline must not double-execute.
    claim = store.begin(
        session_id="s1", step_key="k1", tool_name="t", timeout_seconds=300, now=200
    )
    assert claim.outcome is StepOutcome.RUNNING
    with pytest.raises(ToolStepBusy):
        run_tool_step(
            store,
            session_id="s1",
            step_key="k1",
            tool_name="t",
            run=dict,
            timeout_seconds=300,
            now_fn=lambda: 250,
        )


def test_resume_stale_reclaims_running_step_past_deadline_after_restart(tmp_path) -> None:
    db = f"sqlite:///{tmp_path / 'steps.db'}"
    store = SqlAlchemyToolStepStore(db)
    store.begin(
        session_id="s1",
        step_key="k1",
        tool_name="t",
        max_attempts=3,
        timeout_seconds=60,
        now=100,
    )  # deadline_at = 160

    # Simulate a restart: a fresh store over the same DB, boot sweep at now>deadline.
    rebooted = SqlAlchemyToolStepStore(db)
    reclaimed = rebooted.resume_stale(now=200)
    assert reclaimed == 1
    assert rebooted.get(session_id="s1", step_key="k1").status == "pending"

    # The reclaimed step can be re-claimed for another attempt.
    claim = rebooted.begin(
        session_id="s1", step_key="k1", tool_name="t", now=201
    )
    assert claim.outcome is StepOutcome.CLAIMED
    assert claim.step.attempts == 2


def test_resume_stale_fails_orphaned_step_at_attempt_cap(tmp_path) -> None:
    store = _store(tmp_path)
    store.begin(
        session_id="s1",
        step_key="k1",
        tool_name="t",
        max_attempts=1,
        timeout_seconds=60,
        now=100,
    )  # attempts=1 == max_attempts, deadline_at=160

    assert store.resume_stale(now=200) == 1
    # No attempts remain → the orphaned step is terminally failed, not retried.
    assert store.get(session_id="s1", step_key="k1").status == "failed"


def test_resume_stale_ignores_running_step_within_deadline(tmp_path) -> None:
    store = _store(tmp_path)
    store.begin(
        session_id="s1",
        step_key="k1",
        tool_name="t",
        timeout_seconds=300,
        now=100,
    )  # deadline_at=400
    assert store.resume_stale(now=200) == 0
    assert store.get(session_id="s1", step_key="k1").status == "running"
