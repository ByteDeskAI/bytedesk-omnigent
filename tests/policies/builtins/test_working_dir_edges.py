"""Edge-case coverage for :mod:`omnigent.policies.builtins.working_dir`."""

from __future__ import annotations

import pytest

from omnigent.policies.builtins._shell import MAX_SHELL_NESTING
from omnigent.policies.builtins.working_dir import block_working_dir_changes
from omnigent.policies.schema import PolicyEvent
from tests.policies.builtins.test_working_dir import _sh


def test_cd_empty_target_denied_with_allowed_dirs() -> None:
    """Blank cd target is not under any allowed directory."""
    policy = block_working_dir_changes(allowed_dirs=["/workspace"])
    result = policy(_sh('cd ""'))
    assert result is not None
    assert result["result"] == "DENY"


def test_untokenizable_git_worktree_segment_is_gated() -> None:
    """Unbalanced git worktree syntax is surfaced rather than skipped."""
    policy = block_working_dir_changes()
    result = policy(_sh('git worktree add "/tmp/wt'))
    assert result is not None
    assert result["result"] == "DENY"
    assert "worktree" in result["reason"].lower()


def test_untokenizable_git_dash_c_segment_is_gated() -> None:
    """Unbalanced ``git -C`` syntax is surfaced when cd gating is on."""
    policy = block_working_dir_changes(block_worktree=False)
    result = policy(_sh('git -C "/tmp'))
    assert result is not None
    assert result["result"] == "DENY"


def test_excessive_shell_nesting_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Beyond ``MAX_SHELL_NESTING`` unwrapping stops and abstains."""
    monkeypatch.setattr(
        "omnigent.policies.builtins.working_dir.MAX_SHELL_NESTING",
        0,
    )
    policy = block_working_dir_changes()
    assert policy(_sh('bash -c "cd /etc"')) is None


def test_git_status_without_worktree_or_dash_c_not_flagged_as_dir_op() -> None:
    """Plain ``git status`` on an un-tokenizable segment is not treated as gated."""
    from omnigent.policies.builtins.working_dir import _looks_like_dir_op

    assert _looks_like_dir_op('git status "/bad', block_cd=True, block_worktree=True) is False


def test_env_only_segment_abstains() -> None:
    """A segment that reduces to only env assignments abstains."""
    policy = block_working_dir_changes()
    assert policy(_sh("FOO=bar")) is None


def test_non_dict_tool_call_data_abstains() -> None:
    """Non-dict ``data`` on shell tool_call abstains."""
    policy = block_working_dir_changes()
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": None,
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert policy(event) is None


def test_empty_shell_command_abstains() -> None:
    """Shell tool with blank command abstains."""
    policy = block_working_dir_changes()
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {"command": "   "}},
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    assert policy(event) is None