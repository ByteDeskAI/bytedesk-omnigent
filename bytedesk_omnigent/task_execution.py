"""The "run a Task" execution shim (BDP-2336, ADR-0146).

"Run a Task" is **resolve, then dispatch** — two seams composed, the spawn
engine untouched (ADR-0141 — one engine):

1. **Resolve** the Task's assignee via the assignment resolver (BDP-2335,
   ``find_specialist``: explicit owner → capability ∩ department → scoreboard
   rank). The resolver returns an ``agent_id``.
2. **Dispatch** a fresh root session for that agent seeded with the task as its
   first user turn, by calling the EXISTING session-create / spawn path — the
   :class:`~bytedesk_omnigent.sessions.initiate.SessionInitiator` seam
   (BDP-2279 α3b). The dispatched agent then orchestrates sub-agents exactly as
   today (``sys_session_create`` / ``sys_session_send``); this shim adds no new
   orchestration and modifies no engine.

The shim is a pure adapter over two injected Strategies (ADR-0008) — a
:class:`TaskAssigneeResolver` and a ``SessionInitiator`` — so the
resolve-then-dispatch mapping is unit-provable without a live runtime, the same
posture as :func:`~bytedesk_omnigent.sessions.initiate.build_cron_dispatch`. The
live wiring resolves both from the process-wide registries
(:func:`get_task_assignee_resolver`, :func:`~bytedesk_omnigent.sessions.get_session_initiator`);
until both are registered, :func:`run_task` degrades to a typed
:class:`TaskDispatchError` rather than guessing — the established
fail-closed-on-missing-seam posture.

This module imports B1's ``Task`` (BDP-2333) and B3's resolver (BDP-2335) only
lazily / under ``TYPE_CHECKING`` so it never hard-couples to their concrete
shapes — the shim talks to the ``TaskAssigneeResolver`` protocol and a small set
of documented ``Task`` accessors, both of which any conforming implementation
satisfies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bytedesk_omnigent.sessions.initiate import (
    SessionInitiator,
    get_session_initiator,
)

if TYPE_CHECKING:
    # B1 (BDP-2333): ``Task`` is "a goal with assignment + execution binding"
    # — a frozen dataclass mirroring ``Goal`` (id, owner_agent_id, capability,
    # payload, …). Imported under TYPE_CHECKING only so this shim loads and is
    # unit-provable even before B1 lands; the runtime path takes any object with
    # the documented accessors.
    from bytedesk_omnigent.tasks import Task

_logger = logging.getLogger(__name__)


@runtime_checkable
class TaskAssigneeResolver(Protocol):
    """Resolves a :class:`Task` to the ``agent_id`` that should execute it.

    The assignment resolver (BDP-2335, ``find_specialist``): an explicit task
    owner wins, else the best capability ∩ department match ranked by the ops
    scoreboard. Returns the resolved ``agent_id``, or ``None`` when no agent can
    be assigned (an unassignable task — the caller decides what to do).
    """

    def resolve(self, task: Task) -> str | None:
        """Return the ``agent_id`` assigned to ``task``, or ``None``."""
        ...


# Deploy-time registry, mirroring the SessionInitiator registry: the server
# registers the live resolver at startup; until then run_task fails closed with
# a typed error (the established degrade posture).
_resolver: TaskAssigneeResolver | None = None


def set_task_assignee_resolver(resolver: TaskAssigneeResolver | None) -> None:
    """Register (or clear) the process-wide live :class:`TaskAssigneeResolver`."""
    global _resolver
    _resolver = resolver


def get_task_assignee_resolver() -> TaskAssigneeResolver | None:
    """Return the registered live :class:`TaskAssigneeResolver`, or ``None``."""
    return _resolver


class TaskDispatchError(RuntimeError):
    """``run_task`` could not resolve an assignee or dispatch a session.

    Carries the originating ``task_id`` so the caller can correlate the failure
    with the backlog row that triggered it.
    """

    def __init__(self, message: str, *, task_id: str) -> None:
        super().__init__(message)
        self.task_id = task_id


@dataclass(frozen=True)
class TaskDispatch:
    """The outcome of dispatching a Task — what ran, where it landed.

    :param task_id: The dispatched :class:`Task`'s id.
    :param agent_id: The resolved assignee the session runs as.
    :param session_id: The created session / conversation id (the live session
        the assigned agent now orchestrates sub-agents from).
    """

    task_id: str
    agent_id: str
    session_id: str


def _task_id(task: Task) -> str:
    """Return ``task.id`` — the documented identity field (mirrors ``Goal.id``)."""
    return task.id


def _explicit_owner(task: Task) -> str | None:
    """Return the task's explicitly-assigned ``agent_id``, if any.

    A Task is "a goal with assignment + execution binding": the explicit-owner
    field mirrors ``Goal.owner_agent_id``. The resolver (B3) applies the same
    "explicit owner wins" rule first, so passing an owned task to the resolver
    short-circuits to that owner; reading it here lets ``run_task`` skip the
    resolver entirely for the already-owned case (and keeps the shim correct
    even if the injected resolver is a bare capability ranker).
    """
    return getattr(task, "owner_agent_id", None)


def _canonical_agent_id(value: str) -> str:
    """Resolve a stored agent slug/name to the stable Omnigent ``ag_...`` id."""
    if value.startswith("ag_"):
        return value
    try:
        from omnigent.runtime import get_agent_store

        store = get_agent_store()
        if store.get(value) is not None:
            return value
        by_name = store.get_by_name(value)
        if by_name is not None:
            return by_name.id
    except Exception:  # noqa: BLE001 - runtime may be uninitialized in unit tests
        return value
    return value


def _task_prompt(task: Task) -> str:
    """Derive the seed prompt (the "task as input") for the dispatched session.

    Prefers an explicit ``payload['prompt']`` / ``payload['input']`` (the
    execution-binding payload), then the task ``title``, then a deterministic
    self-describing line so a sparsely-populated task still produces a
    meaningful, non-empty first turn — the same fall-back posture as
    :func:`~bytedesk_omnigent.sessions.initiate._trigger_prompt`.
    """
    payload = getattr(task, "payload", None)
    if isinstance(payload, dict):
        for key in ("prompt", "input"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    title = getattr(task, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    return f"Execute task: {_task_id(task)}"


def run_task(
    task: Task,
    *,
    resolve: TaskAssigneeResolver | None = None,
    initiator: SessionInitiator | None = None,
    external_key: str | None = None,
) -> TaskDispatch:
    """Resolve ``task``'s assignee, then dispatch a session for that agent.

    The whole shim: ask the assignment resolver who owns the work, then start a
    fresh root session for that agent seeded with the task as its first user
    turn via the existing session-create path. The spawn engine is called as a
    black box and is not modified (ADR-0141) — the assigned agent orchestrates
    its sub-agents exactly as today.

    Both Strategies are injectable for testing; when omitted they resolve from
    the process-wide registries (the live deploy path).

    :param task: The :class:`Task` to run (B1, BDP-2333).
    :param resolve: The assignment resolver to use; defaults to the registered
        live :class:`TaskAssigneeResolver`.
    :param initiator: The session-create seam to use; defaults to the registered
        live :class:`~bytedesk_omnigent.sessions.initiate.SessionInitiator`.
    :returns: A :class:`TaskDispatch` describing the resolved agent + session.
    :raises TaskDispatchError: If no resolver / initiator is available, or the
        resolver returns no assignable agent.
    """
    task_id = _task_id(task)

    session_initiator = initiator if initiator is not None else get_session_initiator()
    if session_initiator is None:
        raise TaskDispatchError(
            f"no session initiator registered — cannot dispatch a session for task {task_id!r}",
            task_id=task_id,
        )

    # 1. Resolve: explicit owner short-circuits; otherwise the resolver applies
    #    the full owner → capability ∩ department → scoreboard chain (B3).
    agent_id = _explicit_owner(task)
    if not agent_id:
        resolver = resolve if resolve is not None else get_task_assignee_resolver()
        if resolver is None:
            raise TaskDispatchError(
                "no task assignee resolver registered — cannot resolve an agent "
                f"for task {task_id!r}",
                task_id=task_id,
            )
        agent_id = resolver.resolve(task)
    if not agent_id:
        raise TaskDispatchError(
            f"task {task_id!r} resolved to no assignable agent",
            task_id=task_id,
        )
    agent_id = _canonical_agent_id(agent_id)

    # 2. Dispatch: create + start a session for the resolved agent, seeded with
    #    the task as input — the EXISTING spawn/session-create path, untouched.
    initiate_kwargs = {
        "agent_id": agent_id,
        "prompt": _task_prompt(task),
        "source": f"task:{task_id}",
        "metadata": {"task_id": task_id, "agent_id": agent_id},
    }
    if external_key:
        initiate_kwargs["external_key"] = external_key
    session_id = session_initiator.initiate(**initiate_kwargs)
    _logger.info(
        "run_task dispatched session %s for task=%s agent=%s",
        session_id,
        task_id,
        agent_id,
    )
    return TaskDispatch(task_id=task_id, agent_id=agent_id, session_id=session_id)
