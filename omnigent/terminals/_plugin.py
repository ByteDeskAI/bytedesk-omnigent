"""CORE first-party plugin — the ``omnigent.terminals`` tool family (BDP-2509).

Dogfoods the kernel extension seam for the ``terminals`` subpackage (Section 9.1
of ``docs/EXTENSION_FRAMEWORK_ANALYSIS.md``: the ``omnigent/terminals/`` row,
which registers into the ``tool_factories`` seam with the ``sys_terminal_*``
tools). This is *not* privileged code — it registers through the exact same
:class:`omnigent.kernel.extensions.OmnigentExtension` contract a third-party plugin
would use, via the :mod:`omnigent.sdk` ``@extension`` / ``@tool`` decorators
(the Section 9.2 dogfooding argument). The existing concrete providers in
``omnigent/tools/builtins/sys_terminal.py`` are **reused unchanged** — this
module only *registers* them through the seam; it does not move or rewrite them.

The five providers are the ``sys_terminal_*`` tool classes that the legacy
``ToolManager._register_terminal_tools`` constructs by hand today
(``omnigent/tools/manager.py:668``):

  * ``sys_terminal_launch`` → :class:`SysTerminalLaunchTool`
  * ``sys_terminal_send``   → :class:`SysTerminalSendTool`
  * ``sys_terminal_read``   → :class:`SysTerminalReadTool`
  * ``sys_terminal_list``   → :class:`SysTerminalListTool`
  * ``sys_terminal_close``  → :class:`SysTerminalCloseTool`

All five are thin wrappers around the AP-process
:class:`omnigent.terminals.TerminalRegistry` singleton, which they share so
terminal state survives across turns within a conversation. The registry is
runtime infrastructure (constructed at ``omnigent.runtime._globals.init`` and
read via :func:`omnigent.runtime.get_terminal_registry`); the ``sys_terminal_*``
tools are the seam contribution.

**Boot status.** This plugin is intentionally NOT wired into the composition
root yet — the Integration phase of BDP-2503 does that (it needs the per-
conversation ``AgentSpec`` threaded in, which today flows through
``ToolManager``). For now it only needs to import cleanly and expose correct
seam-hook returns so the dogfooding shape is validated. Constructing the tool
classes is side-effect-free (each ``__init__`` merely stashes its collaborators),
so building them at registration time is safe even when the spec is absent —
``SysTerminalLaunchTool`` dereferences its spec only inside ``invoke``.

**Circular-import safety (Section 6 / Rule 4).** Every domain import — the tool
classes, the registry accessor — is deferred *inside* the hook/factory bodies so
importing this module (and therefore :mod:`omnigent.sdk`) stays kernel-light and
cannot pull the FastAPI / tool stack onto a hot import path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omnigent.sdk import extension, tool

if TYPE_CHECKING:  # type-only — never imported at runtime (kernel-light)
    from omnigent.spec.types import AgentSpec
    from omnigent.terminals import TerminalRegistry
    from omnigent.tools.base import Tool


def _terminal_registry() -> "TerminalRegistry":
    """Return the AP-process :class:`TerminalRegistry` singleton.

    Deferred import keeps the kernel/SDK import path free of the terminal
    runtime. Mirrors how ``ToolManager._register_terminal_tools`` resolves the
    shared registry (``omnigent/tools/manager.py:667``) — the same singleton, so
    tools registered through this seam see the same per-conversation state.
    """
    from omnigent.runtime import get_terminal_registry

    return get_terminal_registry()


@extension(name="omnigent.terminals")
class TerminalsExtension:
    """First-party plugin contributing the ``sys_terminal_*`` tool family.

    Registers into the ``tool_factories`` kernel seam. Each ``@tool`` method is
    a factory the kernel calls as ``factory(config) -> Tool``; the synthesised
    ``tool_factories()`` returns ``{name: factory}`` for all five — the same
    shape ``ToolManager`` builds inline today, but discovered through the seam.
    """

    @tool(name="sys_terminal_launch")
    def sys_terminal_launch(self, spec: "AgentSpec | None" = None) -> "Tool":
        """``sys_terminal_launch`` — start a configured tmux session.

        The launch tool is the only one that needs the per-conversation
        :class:`AgentSpec` (for terminal-name / override-flag lookup). It is
        injected when available; ``None`` is tolerated at construction because
        :class:`SysTerminalLaunchTool` dereferences the spec only inside
        ``invoke`` — so the seam shape is exercisable before boot wires the spec.
        """
        from omnigent.tools.builtins.sys_terminal import SysTerminalLaunchTool

        return SysTerminalLaunchTool(spec=spec, registry=_terminal_registry())

    @tool(name="sys_terminal_send")
    def sys_terminal_send(self) -> "Tool":
        """``sys_terminal_send`` — type text + key chords into a terminal."""
        from omnigent.tools.builtins.sys_terminal import SysTerminalSendTool

        return SysTerminalSendTool(registry=_terminal_registry())

    @tool(name="sys_terminal_read")
    def sys_terminal_read(self) -> "Tool":
        """``sys_terminal_read`` — capture the visible pane + scrollback."""
        from omnigent.tools.builtins.sys_terminal import SysTerminalReadTool

        return SysTerminalReadTool(registry=_terminal_registry())

    @tool(name="sys_terminal_list")
    def sys_terminal_list(self) -> "Tool":
        """``sys_terminal_list`` — enumerate the conversation's terminals."""
        from omnigent.tools.builtins.sys_terminal import SysTerminalListTool

        return SysTerminalListTool(registry=_terminal_registry())

    @tool(name="sys_terminal_close")
    def sys_terminal_close(self) -> "Tool":
        """``sys_terminal_close`` — kill a session and drop it from the registry."""
        from omnigent.tools.builtins.sys_terminal import SysTerminalCloseTool

        return SysTerminalCloseTool(registry=_terminal_registry())


#: The expected ``sys_terminal_*`` names this plugin contributes through the
#: ``tool_factories`` seam — the same five names ``ToolManager`` registers today.
#: Exposed for tests / integration wiring to assert the seam shape without
#: instantiating the (registry-dependent) tools.
TERMINAL_TOOL_NAMES: tuple[str, ...] = (
    "sys_terminal_launch",
    "sys_terminal_send",
    "sys_terminal_read",
    "sys_terminal_list",
    "sys_terminal_close",
)


__all__ = ["TerminalsExtension", "TERMINAL_TOOL_NAMES"]


# Re-export under a module-level alias so callers/tests can refer to the plugin
# class generically without hard-coding the per-subpackage class name.
Plugin: Any = TerminalsExtension
