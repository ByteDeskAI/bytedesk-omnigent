"""Tests for first-class-task governance via the spawn-breadth governor (BDP-2338).

Task governance does NOT introduce a new governance engine — it reuses the
existing :func:`spawn_breadth_governor` breadth cap (BDP-2272, ADR-0142) and
adds one OPTIONAL per-task spawn cap. When ``per_task_max_spawns`` is set and a
``sys_session_create`` is attributed to a first-class task (``arguments.task_id``),
the governor additionally counts spawns per task in a task-scoped
``session_state`` counter (``_policy_spawn_count_task:{task_id}``) and DENYs once
the per-task limit is reached — using the exact same counter / increment / DENY
mechanism as the session-wide breadth cap.

These tests pin the additive behavior: the per-task cap is off by default (the
legacy breadth-only path is unchanged), and when on it bounds spawns per task
independently of the session-wide cap.
"""
from __future__ import annotations

from bytedesk_omnigent.policies.spawn_governor import (
    POLICY_REGISTRY,
    spawn_breadth_governor,
)


def _spawn_event(
    session_count: int = 0,
    *,
    task_id: str | None = None,
    task_count: int | None = None,
) -> dict:
    """Build a ``sys_session_create`` tool_call event.

    :param session_count: Current session-wide spawn counter value.
    :param task_id: First-class task the spawn is attributed to, surfaced as
        ``arguments.task_id`` (omitted when ``None``).
    :param task_count: Current per-task spawn counter value to seed in
        ``session_state`` under the task-scoped key (omitted when ``None``).
    """
    arguments: dict = {}
    if task_id is not None:
        arguments["task_id"] = task_id
    state: dict = {"_policy_spawn_count": session_count}
    if task_id is not None and task_count is not None:
        state[f"_policy_spawn_count_task:{task_id}"] = task_count
    return {
        "type": "tool_call",
        "data": {"name": "sys_session_create", "arguments": arguments},
        "session_state": state,
    }


def test_per_task_cap_off_by_default_preserves_breadth_only_behavior() -> None:
    # No ``per_task_max_spawns`` => legacy breadth-only governor: a task-attributed
    # spawn well under the session cap ALLOWs and bumps only the session counter.
    gov = spawn_breadth_governor(max_spawns=8)

    resp = gov(_spawn_event(0, task_id="BDP-2338", task_count=99))
    assert resp["result"] == "ALLOW"
    keys = {u["key"] for u in resp["state_updates"]}
    assert keys == {"_policy_spawn_count"}  # no per-task counter when cap is off


def test_per_task_cap_allows_under_limit_and_increments_task_counter() -> None:
    gov = spawn_breadth_governor(max_spawns=100, per_task_max_spawns=2)

    resp = gov(_spawn_event(0, task_id="BDP-2338", task_count=0))
    assert resp["result"] == "ALLOW"
    keys = {u["key"] for u in resp["state_updates"]}
    # Both the session-wide and the task-scoped counters are bumped on ALLOW.
    assert "_policy_spawn_count" in keys
    assert "_policy_spawn_count_task:BDP-2338" in keys
    for upd in resp["state_updates"]:
        assert upd["action"] == "increment"


def test_per_task_cap_denies_at_task_limit_even_under_session_limit() -> None:
    # Session cap is generous; the per-task cap is the binding constraint.
    gov = spawn_breadth_governor(max_spawns=100, per_task_max_spawns=3)

    assert gov(_spawn_event(0, task_id="t1", task_count=2))["result"] == "ALLOW"

    denied = gov(_spawn_event(0, task_id="t1", task_count=3))
    assert denied["result"] == "DENY"
    assert "t1" in denied["reason"]
    assert "per-task" in denied["reason"]


def test_per_task_cap_is_scoped_per_task_id() -> None:
    gov = spawn_breadth_governor(max_spawns=100, per_task_max_spawns=1)

    # Task ``a`` is already at its per-task limit ...
    assert gov(_spawn_event(0, task_id="a", task_count=1))["result"] == "DENY"
    # ... but task ``b`` (independent counter) is still free to spawn.
    assert gov(_spawn_event(0, task_id="b", task_count=0))["result"] == "ALLOW"


def test_untagged_spawn_skips_per_task_cap_but_still_breadth_capped() -> None:
    # A spawn with no ``task_id`` is not attributed to a first-class task, so the
    # per-task cap does not apply — only the session-wide breadth cap governs it.
    gov = spawn_breadth_governor(max_spawns=2, per_task_max_spawns=1)

    resp = gov(_spawn_event(0))  # no task_id
    assert resp["result"] == "ALLOW"
    keys = {u["key"] for u in resp["state_updates"]}
    assert keys == {"_policy_spawn_count"}  # no per-task counter for untagged spawns

    # The session-wide breadth cap still fires for an untagged spawn at the limit.
    assert gov(_spawn_event(2))["result"] == "DENY"


def test_session_breadth_cap_still_wins_over_an_unfilled_task_cap() -> None:
    # When the session-wide cap is the binding constraint it DENYs first, even if
    # the per-task counter is well under its own limit.
    gov = spawn_breadth_governor(max_spawns=1, per_task_max_spawns=10)

    denied = gov(_spawn_event(1, task_id="t1", task_count=0))
    assert denied["result"] == "DENY"
    assert "spawn-breadth governor" in denied["reason"]


def test_registry_exposes_optional_per_task_max_spawns_param() -> None:
    entry = POLICY_REGISTRY[0]
    props = entry["params_schema"]["properties"]
    assert "per_task_max_spawns" in props
    # Optional knob: not in ``required`` so existing attachments stay valid.
    assert "per_task_max_spawns" not in entry["params_schema"].get("required", [])
