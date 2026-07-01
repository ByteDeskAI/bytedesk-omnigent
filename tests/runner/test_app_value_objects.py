"""Runner app value-object constructor regressions."""

from __future__ import annotations

from pathlib import Path

from omnigent.runner.app import (
    ResolvedSpec,
    TurnDispatch,
    _ChildParentMeta,
    _CodexNativeLaunchConfig,
    _PiNativeLaunchConfig,
    _SessionSnapshot,
    _SubagentDeliveryAck,
    _SubagentWorkEntry,
)
from omnigent.runner.subagent_status import SubagentWorkStatus


def test_runner_value_objects_accept_keyword_construction(tmp_path: Path) -> None:
    """Value objects remain keyword-constructible after app module splitting."""
    resolved = ResolvedSpec(spec="spec", workdir=tmp_path)
    assert resolved.spec == "spec"
    assert resolved.workdir == tmp_path

    snapshot = _SessionSnapshot(
        ok=True,
        status_code=200,
        created_at=1.0,
        workspace=str(tmp_path),
        agent_id="ag_1",
    )
    assert snapshot.agent_id == "ag_1"

    dispatch = TurnDispatch(client_side_tool_names=frozenset({"Read"}))
    dispatch.client_side_tool_names = frozenset({"Write"})
    assert dispatch.client_side_tool_names == frozenset({"Write"})

    work = _SubagentWorkEntry(
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        work_id="subagent_1",
        agent="researcher",
        title="Research",
    )
    work.status = SubagentWorkStatus.RUNNING
    assert work.status == SubagentWorkStatus.RUNNING

    ack = _SubagentDeliveryAck(entry=work, delivered=True, delivered_now=True, reason="delivered")
    assert ack.entry is work

    meta = _ChildParentMeta(
        parent_id="conv_parent",
        title="Research",
        tool="researcher",
        session_name="research",
    )
    meta.last_busy = True
    assert meta.last_busy is True

    codex = _CodexNativeLaunchConfig(
        workspace=tmp_path,
        policy_server_url="http://server",
        terminal_launch_args=None,
        model_override=None,
        external_session_id=None,
        fork_source_id=None,
        fork_source_external_id=None,
        fork_carry_history=False,
    )
    assert codex.workspace == tmp_path

    pi = _PiNativeLaunchConfig(
        workspace=tmp_path,
        server_url="http://server",
        terminal_launch_args=None,
        external_session_id=None,
    )
    assert pi.server_url == "http://server"
