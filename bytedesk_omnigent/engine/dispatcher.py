"""Goal Dispatcher: a ready goal → one live agent session (BDP-2583, ADR-0142).

``dispatch_goal`` is the keystone — until now a goal that became *ready* (or a
cron trigger that fired) did not actually start agent work. This opens a root
session bound to the goal's owning agent and posts the goal as the opening intent
message, exactly like the planner session start (``routes/goals.py``).

Idempotency (ADR-0009): exactly one live session per ``(goal_id, period_key)``.
``period_key`` is the goal id for immediate / until_done goals, or
``"{goal_id}:{next_fire_at}"`` for a recurring goal so each fire gets its own
session. The session's ``external_key`` is ``"goal:{period_key}"`` — that column
has a UNIQUE constraint (``uq_conversations_external_key``), so a check-then-create
is backstopped by the DB: a racing second dispatch resolves the existing session
instead of spawning a duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import now_epoch
from omnigent.entities import MessageData, NewConversationItem


class ConversationSpawnPort(Protocol):
    """The slice of the conversation store ``dispatch_goal`` depends on (ADR-0008).

    A Protocol so the dispatcher is unit-testable with an in-memory fake — the
    real implementation is ``omnigent``'s ``SqlAlchemyConversationStore``.
    """

    def get_conversation_by_external_key(self, external_key: str) -> Any | None: ...

    def create_conversation(self, **kwargs: Any) -> Any: ...

    def append(self, conversation_id: str, items: Any) -> None: ...


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of one ``dispatch_goal`` call."""

    spawned: bool
    session_id: str | None
    period_key: str


def _intent_message(goal: Any) -> str:
    """The opening message posted into the spawned session."""
    lines = [
        f"You own goal '{goal.title}'.",
        f"Goal id: {goal.id}",
        f"Target: {goal.target_kind}/{goal.target_id}"
        + (f" ({goal.target_label})" if goal.target_label else ""),
        "",
        "Work this goal to completion. When done, mark it done; if blocked, mark it blocked.",
    ]
    return "\n".join(lines)


def dispatch_goal(
    goal: Any,
    *,
    conversation_store: ConversationSpawnPort,
    goal_store: Any,
    now: int | None = None,
    period_key: str | None = None,
) -> DispatchResult:
    """Spawn one live agent session for ``goal``, idempotent within its period.

    :param goal: a :class:`~bytedesk_omnigent.goals.Goal`.
    :param period_key: the idempotency period. Defaults to ``goal.id`` (immediate /
        until_done — one session per goal); a recurring caller passes
        ``f"{goal.id}:{next_fire_at}"`` so each fire gets its own session.
    :returns: a :class:`DispatchResult`; ``spawned`` is ``False`` when a session
        already exists for the period or the goal has no owner.
    """
    del goal_store  # reserved for future assignment integration (see below)
    now = now_epoch() if now is None else now
    period_key = period_key or goal.id

    owner = goal.owner_agent_id
    if not owner:
        # ponytail: no auto-assignment in Phase 1 — an unowned goal is left for
        # assignment.py to claim, then a later dispatch picks it up.
        # TODO(BDP-2583): hook bytedesk_omnigent.assignment to pick an owner here.
        return DispatchResult(spawned=False, session_id=None, period_key=period_key)

    external_key = f"goal:{period_key}"
    existing = conversation_store.get_conversation_by_external_key(external_key)
    if existing is not None:
        return DispatchResult(spawned=False, session_id=existing.id, period_key=period_key)

    try:
        conv = conversation_store.create_conversation(
            agent_id=owner,
            title=f"Goal: {goal.title}",
            kind="default",
            external_key=external_key,
        )
    except IntegrityError:
        # A concurrent dispatch won the UNIQUE external_key (ADR-0009 single live
        # session per period) — resolve to the session it created, no-op here.
        winner = conversation_store.get_conversation_by_external_key(external_key)
        return DispatchResult(
            spawned=False,
            session_id=winner.id if winner is not None else None,
            period_key=period_key,
        )

    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": _intent_message(goal)}],
                ),
                created_by=owner,
            )
        ],
    )
    return DispatchResult(spawned=True, session_id=conv.id, period_key=period_key)
