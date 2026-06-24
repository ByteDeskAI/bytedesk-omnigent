"""Edge-case coverage for :mod:`omnigent.policies.builtins._shell`."""

from __future__ import annotations

from omnigent.policies.builtins._shell import unwrap_shell_command


def test_unwrap_shell_command_returns_none_without_dash_c_flag() -> None:
    """Shell interpreters without ``-c`` do not expose an inner command."""
    assert unwrap_shell_command(["bash", "script.sh"]) is None