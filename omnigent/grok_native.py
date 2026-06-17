"""Native xAI Grok Build CLI (terminal-first) agent spec for Omnigent.

ByteDesk addition mirroring :mod:`omnigent.codex_native`: the "Grok" picker
option runs the Grok Build CLI (ACP over ``grok agent stdio``) with
subscription OAuth (``~/.grok/auth.json``) and renders terminal-first like
Codex / Claude (``omnigent.ui == "terminal"`` is stamped at session creation
because :data:`omnigent.native_coding_agents.GROK_NATIVE_CODING_AGENT` is in
``NATIVE_CODING_AGENTS``).

This module provides the agent-spec materializer the server seeds
(``_build_grok_native_bundle`` in :mod:`omnigent.server.app`). The actual
turn execution + (eventual) terminal-TUI leader-attach bridge live in the
``grok-native`` harness executor (:mod:`omnigent.inner.grok_native_executor`).
The spec mirrors codex-native's: caller-process / no-sandbox os_env (the native
CLI already runs unsandboxed on the workspace) and a default ``shell`` terminal
so the relay advertises the ``sys_terminal_*`` family to the wrapped Grok.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnigent.native_coding_agents import GROK_NATIVE_CODING_AGENT

_AGENT_NAME = GROK_NATIVE_CODING_AGENT.agent_name
_HARNESS = GROK_NATIVE_CODING_AGENT.harness


def _materialize_grok_agent_spec(
    tmpdir: Path,
    *,
    model: str | None,
) -> Path:
    """
    Write the terminal-first agent spec used by the Grok picker option.

    :param tmpdir: Temporary directory for the generated YAML file.
    :param model: Optional model id, e.g. ``"grok-build"``. ``None`` lets the
        Grok CLI pick its default.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / f"{_AGENT_NAME}.yaml"
    executor: dict[str, str] = {"harness": _HARNESS}
    if model is not None:
        executor["model"] = model
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "Grok is running in the session terminal. Web UI messages are "
            "forwarded into the same native Grok session."
        ),
        "executor": executor,
        # Opt the native session into child-session spawn writes so the wrapped
        # Grok can author + launch sub-agent sessions (relay derives its tool
        # set from this spec via ToolManager). Mirrors codex-native.
        "spawn": True,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        # Non-empty ``terminals:`` is the relay's gate for advertising the
        # ``sys_terminal_*`` family to the wrapped Grok CLI.
        "terminals": {
            "shell": {
                "command": "bash",
                "allow_cwd_override": True,
                "os_env": {
                    "type": "caller_process",
                    "cwd": ".",
                    "sandbox": {"type": "none"},
                },
            },
        },
    }
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return yaml_path

