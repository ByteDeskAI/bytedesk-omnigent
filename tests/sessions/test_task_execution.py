"""Tests for the "run a Task" execution shim: resolve → dispatch, spawn
engine untouched (BDP-2336, ADR-0146).

The shim composes two seams — the assignment resolver (B3) and the existing
session-create / spawn path (the ``SessionInitiator`` seam). Both are mocked
here so the test proves the *mapping* (run_task resolves, then dispatches a
session for the resolved agent with the task as input) without a live runtime,
and asserts the spawn-engine call shape is exactly the established
``initiate(agent_id, prompt, source, metadata)`` contract — unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from bytedesk_omnigent.task_execution import (
    TaskAssigneeResolver,
    TaskDispatch,
    TaskDispatchError,
    get_task_assignee_resolver,
    run_task,
    set_task_assignee_resolver,
)

# ── doubles ───────────────────────────────────────────────────────────────


@dataclass
class _StubTask:
    """A minimal stand-in for B1's ``Task`` (BDP-2333) — the documented
    accessors the shim reads: id, owner_agent_id, title, payload."""

    id: str = "task_1"
    owner_agent_id: str | None = None
    title: str | None = None
    payload: dict | None = None


class _RecordingResolver:
    """Records resolve() calls and returns a pre-set agent_id."""

    def __init__(self, agent_id: str | None) -> None:
        self._agent_id = agent_id
        self.calls: list[_StubTask] = []

    def resolve(self, task: _StubTask) -> str | None:
        self.calls.append(task)
        return self._agent_id


@dataclass
class _RecordingInitiator:
    """Records initiate() calls — the existing session-create / spawn path.

    Asserting against ``calls`` proves the spawn engine is invoked with the
    unchanged ``(agent_id, prompt, source, metadata)`` contract, not modified.
    """

    calls: list[dict] = field(default_factory=list)

    def initiate(self, *, agent_id, prompt, source, metadata=None) -> str:
        self.calls.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "source": source,
                "metadata": metadata,
            }
        )
        return f"sess_{len(self.calls)}"


# ── happy path: resolve then dispatch ─────────────────────────────────────


def test_run_task_resolves_then_dispatches_session() -> None:
    resolver = _RecordingResolver("ag_writer")
    initiator = _RecordingInitiator()
    task = _StubTask(
        id="task_seo",
        title="Run the comprehensive SEO report",
        payload={"prompt": "Generate the comprehensive SEO report for acme.com."},
    )

    result = run_task(task, resolve=resolver, initiator=initiator)

    # Resolved first…
    assert resolver.calls == [task]
    # …then dispatched a session for the resolved agent with the task as input.
    assert len(initiator.calls) == 1
    call = initiator.calls[0]
    assert call["agent_id"] == "ag_writer"
    assert call["prompt"] == "Generate the comprehensive SEO report for acme.com."
    assert call["source"] == "task:task_seo"
    assert call["metadata"] == {"task_id": "task_seo", "agent_id": "ag_writer"}

    assert result == TaskDispatch(task_id="task_seo", agent_id="ag_writer", session_id="sess_1")


def test_run_task_dispatch_contract_is_unchanged_initiate_shape() -> None:
    """The spawn-engine call is the established initiate() keyword contract —
    exactly the four keywords, nothing added/removed (engine untouched)."""
    resolver = _RecordingResolver("ag_1")
    initiator = _RecordingInitiator()

    run_task(_StubTask(id="t1", title="do the thing"), resolve=resolver, initiator=initiator)

    (call,) = initiator.calls
    assert set(call) == {"agent_id", "prompt", "source", "metadata"}


# ── explicit owner short-circuits the resolver ────────────────────────────


def test_run_task_explicit_owner_wins_without_resolver_call() -> None:
    resolver = _RecordingResolver("ag_ranked")  # would rank a different agent
    initiator = _RecordingInitiator()
    task = _StubTask(id="t_owned", owner_agent_id="ag_owner", title="owned work")

    result = run_task(task, resolve=resolver, initiator=initiator)

    # Explicit owner wins; the resolver is never consulted.
    assert resolver.calls == []
    assert result.agent_id == "ag_owner"
    assert initiator.calls[0]["agent_id"] == "ag_owner"


# ── seed-prompt derivation (task as input) ────────────────────────────────


def test_run_task_prompt_falls_back_title_then_self_describing() -> None:
    resolver = _RecordingResolver("ag_1")
    initiator = _RecordingInitiator()

    # payload-less task → falls back to the title…
    run_task(
        _StubTask(id="t_title", title="  Draft the brief  "),
        resolve=resolver,
        initiator=initiator,
    )
    # …title-less + payload-less task → deterministic self-describing line.
    run_task(_StubTask(id="t_bare"), resolve=resolver, initiator=initiator)

    assert [c["prompt"] for c in initiator.calls] == [
        "Draft the brief",
        "Execute task: t_bare",
    ]


# ── fail-closed when a seam or an assignee is missing ─────────────────────


def test_run_task_raises_when_no_resolver() -> None:
    initiator = _RecordingInitiator()
    with pytest.raises(TaskDispatchError) as exc:
        run_task(_StubTask(id="t_x"), resolve=None, initiator=initiator)
    assert exc.value.task_id == "t_x"
    # never reached dispatch
    assert initiator.calls == []


def test_run_task_raises_when_no_initiator() -> None:
    resolver = _RecordingResolver("ag_1")
    with pytest.raises(TaskDispatchError) as exc:
        run_task(_StubTask(id="t_y"), resolve=resolver, initiator=None)
    assert exc.value.task_id == "t_y"


def test_run_task_raises_when_resolver_returns_no_agent() -> None:
    resolver = _RecordingResolver(None)  # unassignable
    initiator = _RecordingInitiator()
    with pytest.raises(TaskDispatchError) as exc:
        run_task(_StubTask(id="t_z"), resolve=resolver, initiator=initiator)
    assert exc.value.task_id == "t_z"
    # resolved (and failed) before any dispatch — spawn engine never called.
    assert initiator.calls == []


# ── registry round-trip + default-from-registry path ──────────────────────


def test_resolver_registry_set_and_get_round_trip() -> None:
    resolver = _RecordingResolver("ag_1")
    try:
        assert get_task_assignee_resolver() is None
        set_task_assignee_resolver(resolver)
        assert get_task_assignee_resolver() is resolver
    finally:
        set_task_assignee_resolver(None)
    assert get_task_assignee_resolver() is None


def test_run_task_defaults_to_registered_resolver() -> None:
    resolver = _RecordingResolver("ag_reg")
    initiator = _RecordingInitiator()
    try:
        set_task_assignee_resolver(resolver)
        # resolve omitted → pulls the registered live resolver.
        result = run_task(_StubTask(id="t_reg", title="x"), initiator=initiator)
    finally:
        set_task_assignee_resolver(None)
    assert resolver.calls and result.agent_id == "ag_reg"


def test_recording_resolver_satisfies_protocol() -> None:
    """The injected double conforms to the runtime-checkable resolver protocol."""
    assert isinstance(_RecordingResolver("ag_1"), TaskAssigneeResolver)
