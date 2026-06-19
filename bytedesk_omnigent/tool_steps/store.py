"""Durable deterministic tool-step store (BDP-2252 α5, ADR-0142).

A single-writer store over the ``tool_steps`` table, sharing the conversation
store's engine exactly like the signal bus (``omnigent/bus/``) and cron scheduler
(``omnigent/scheduler/``). All state transitions are guarded UPDATEs so concurrent
/ replayed claims are idempotent (ADR-0009 single-writer).

Lifecycle of a ``(session_id, step_key)``::

    begin() -> CLAIMED (status=running, attempts+=1, deadline_at=now+timeout)
        run the tool
        complete(result)  -> status=completed, result cached
      or
        fail(error)       -> retrying (status=pending, attempts<max)
                          or  exhausted (status=failed, attempts>=max)
    begin() again         -> ALREADY_COMPLETED (returns cached result; no re-run)
                          or  EXHAUSTED (all attempts spent)
                          or  CLAIMED (a pending retry, or a running step whose
                              deadline_at passed — reclaimed after a restart)

``resume_stale()`` is the boot sweep: a ``running`` step past its ``deadline_at``
(its worker crashed / the process restarted) goes back to ``pending`` if attempts
remain, else ``failed``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select, update

from bytedesk_omnigent.db_models import SqlToolStep
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)


class StepOutcome(str, Enum):
    """The outcome of a :meth:`SqlAlchemyToolStepStore.begin` claim."""

    CLAIMED = "claimed"
    ALREADY_COMPLETED = "already_completed"
    RUNNING = "running"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class ToolStep:
    """A tool-step row (decoded ``result`` from JSON)."""

    id: str
    session_id: str
    step_key: str
    tool_name: str
    status: str
    attempts: int
    max_attempts: int
    timeout_seconds: int | None
    deadline_at: int | None
    result: object | None
    error: str | None


@dataclass(frozen=True)
class StepClaim:
    """The result of :meth:`SqlAlchemyToolStepStore.begin`."""

    outcome: StepOutcome
    step: ToolStep


class ToolStepExhausted(RuntimeError):
    """A tool-step failed every attempt (``status=failed``)."""

    def __init__(self, step_key: str, error: str | None) -> None:
        super().__init__(f"tool-step {step_key!r} exhausted: {error}")
        self.step_key = step_key
        self.error = error


class ToolStepBusy(RuntimeError):
    """A tool-step is actively ``running`` elsewhere (not yet past its deadline)."""

    def __init__(self, step_key: str) -> None:
        super().__init__(f"tool-step {step_key!r} is already running")
        self.step_key = step_key


def _to_step(row: SqlToolStep) -> ToolStep:
    return ToolStep(
        id=row.id,
        session_id=row.session_id,
        step_key=row.step_key,
        tool_name=row.tool_name,
        status=row.status,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        timeout_seconds=row.timeout_seconds,
        deadline_at=row.deadline_at,
        result=json.loads(row.result) if row.result is not None else None,
        error=row.error,
    )


class SqlAlchemyToolStepStore:
    """Durable deterministic tool-step store (ADR-0142). See the module docstring.

    :param storage_location: SQLAlchemy database URI (the same engine the
        conversation store uses).
    """

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        # immediate=True: SQLite write-lock-before-read so claim cannot race
        # (ADR-0009 single-writer); no-op on PostgreSQL.
        self._write_session = make_managed_session_maker(self._engine, immediate=True)

    @property
    def engine(self):
        """The underlying SQLAlchemy engine (used for advisory-lock coordination)."""
        return self._engine

    def begin(
        self,
        *,
        session_id: str,
        step_key: str,
        tool_name: str,
        max_attempts: int = 1,
        timeout_seconds: int | None = None,
        now: int | None = None,
    ) -> StepClaim:
        """Idempotently claim ``(session_id, step_key)`` for execution.

        First claim inserts a ``running`` row (``attempts=1``); a replay returns
        the cached terminal state (``ALREADY_COMPLETED`` / ``EXHAUSTED``); a
        ``pending`` retry or a ``running`` step past its ``deadline_at`` is
        re-claimed (``attempts`` incremented). A ``running`` step still within its
        deadline returns ``RUNNING`` (do not double-execute).
        """
        now = now_epoch() if now is None else now
        deadline = now + timeout_seconds if timeout_seconds is not None else None
        with self._write_session() as session:
            existing = session.execute(
                select(SqlToolStep).where(
                    SqlToolStep.session_id == session_id,
                    SqlToolStep.step_key == step_key,
                )
            ).scalar_one_or_none()

            if existing is None:
                row = SqlToolStep(
                    id=f"step_{uuid.uuid4().hex}",
                    session_id=session_id,
                    step_key=step_key,
                    tool_name=tool_name,
                    status="running",
                    attempts=1,
                    max_attempts=max_attempts,
                    timeout_seconds=timeout_seconds,
                    deadline_at=deadline,
                    created_at=now,
                    started_at=now,
                )
                session.add(row)
                session.flush()
                return StepClaim(StepOutcome.CLAIMED, _to_step(row))

            if existing.status == "completed":
                return StepClaim(StepOutcome.ALREADY_COMPLETED, _to_step(existing))
            if existing.status == "failed":
                return StepClaim(StepOutcome.EXHAUSTED, _to_step(existing))

            running_past_deadline = (
                existing.status == "running"
                and existing.deadline_at is not None
                and existing.deadline_at <= now
            )
            if existing.status == "running" and not running_past_deadline:
                return StepClaim(StepOutcome.RUNNING, _to_step(existing))

            # An orphaned ``running`` step past its deadline that has ALREADY spent
            # all its attempts must not be re-claimed — reclaiming would execute it
            # past ``max_attempts``. Terminalize it instead, mirroring fail() /
            # resume_stale() (which is the only other path that reclaims it).
            if existing.attempts >= existing.max_attempts:
                existing.status = "failed"
                existing.error = existing.error or "reclaim: attempts exhausted"
                existing.completed_at = now
                session.flush()
                return StepClaim(StepOutcome.EXHAUSTED, _to_step(existing))

            # pending retry, or a running step orphaned past its deadline → reclaim.
            existing.status = "running"
            existing.attempts = existing.attempts + 1
            existing.started_at = now
            existing.deadline_at = deadline
            existing.error = None
            session.flush()
            return StepClaim(StepOutcome.CLAIMED, _to_step(existing))

    def complete(
        self,
        *,
        session_id: str,
        step_key: str,
        result: object | None,
        now: int | None = None,
    ) -> bool:
        """Guarded ``running -> completed`` with the cached ``result``.

        Returns ``True`` when this caller owned the running claim (``rowcount==1``),
        ``False`` on a lost/replayed completion.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            res = session.execute(
                update(SqlToolStep)
                .where(
                    SqlToolStep.session_id == session_id,
                    SqlToolStep.step_key == step_key,
                    SqlToolStep.status == "running",
                )
                .values(
                    status="completed",
                    result=json.dumps(result) if result is not None else None,
                    error=None,
                    completed_at=now,
                )
            )
            return res.rowcount == 1

    def fail(
        self,
        *,
        session_id: str,
        step_key: str,
        error: str,
        now: int | None = None,
    ) -> str:
        """Record a failed attempt.

        Returns ``"retrying"`` when attempts remain (``status -> pending``) or
        ``"exhausted"`` at the cap (``status -> failed``).
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            row = session.execute(
                select(SqlToolStep).where(
                    SqlToolStep.session_id == session_id,
                    SqlToolStep.step_key == step_key,
                )
            ).scalar_one_or_none()
            if row is None:
                return "exhausted"
            if row.attempts < row.max_attempts:
                row.status = "pending"
                row.error = error
                row.deadline_at = None
                session.flush()
                return "retrying"
            row.status = "failed"
            row.error = error
            row.completed_at = now
            session.flush()
            return "exhausted"

    def resume_stale(self, *, now: int | None = None) -> int:
        """Boot sweep: reclaim ``running`` steps past their ``deadline_at``.

        A step orphaned by a crash / restart goes back to ``pending`` (attempts
        remain) or ``failed`` (at the cap). Returns the number reclaimed.
        """
        now = now_epoch() if now is None else now
        with self._write_session() as session:
            stale = session.execute(
                select(SqlToolStep).where(
                    SqlToolStep.status == "running",
                    SqlToolStep.deadline_at.is_not(None),
                    SqlToolStep.deadline_at <= now,
                )
            ).scalars().all()
            reclaimed = 0
            for row in stale:
                if row.attempts < row.max_attempts:
                    row.status = "pending"
                    row.deadline_at = None
                else:
                    row.status = "failed"
                    row.error = row.error or "resume: deadline exceeded after restart"
                    row.completed_at = now
                reclaimed += 1
            session.flush()
            return reclaimed

    def get(self, *, session_id: str, step_key: str) -> ToolStep | None:
        """Return the current state of a step (or ``None``)."""
        with self._session() as session:
            row = session.execute(
                select(SqlToolStep).where(
                    SqlToolStep.session_id == session_id,
                    SqlToolStep.step_key == step_key,
                )
            ).scalar_one_or_none()
            return _to_step(row) if row is not None else None


def run_tool_step(
    store: SqlAlchemyToolStepStore,
    *,
    session_id: str,
    step_key: str,
    tool_name: str,
    run,
    max_attempts: int = 1,
    timeout_seconds: int | None = None,
    now_fn=now_epoch,
):
    """Deterministic durable tool-step: claim → execute → record, with retry.

    ``run`` is a zero-arg callable returning a JSON-serializable result. On a
    replay of an already-completed step the cached result is returned **without
    re-executing** (deterministic, no double side effect). A failure retries (up
    to ``max_attempts``); exhaustion raises :class:`ToolStepExhausted`. A step
    actively running elsewhere raises :class:`ToolStepBusy`.

    Wall-clock timeout enforcement for a single attempt is the caller's concern
    (wrap ``run`` with ``omnigent.runtime.tool_retry.call_tool_with_timeout``);
    ``timeout_seconds`` here sets the durable ``deadline_at`` that
    :meth:`SqlAlchemyToolStepStore.resume_stale` reclaims after a restart.
    """
    while True:
        claim = store.begin(
            session_id=session_id,
            step_key=step_key,
            tool_name=tool_name,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            now=now_fn(),
        )
        if claim.outcome is StepOutcome.ALREADY_COMPLETED:
            return claim.step.result
        if claim.outcome is StepOutcome.EXHAUSTED:
            raise ToolStepExhausted(step_key, claim.step.error)
        if claim.outcome is StepOutcome.RUNNING:
            raise ToolStepBusy(step_key)

        try:
            result = run()
        except Exception as exc:
            outcome = store.fail(
                session_id=session_id, step_key=step_key, error=str(exc), now=now_fn()
            )
            if outcome == "exhausted":
                raise ToolStepExhausted(step_key, str(exc)) from exc
            continue
        store.complete(
            session_id=session_id, step_key=step_key, result=result, now=now_fn()
        )
        return result
