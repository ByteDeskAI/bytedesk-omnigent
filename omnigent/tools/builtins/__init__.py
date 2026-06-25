"""Built-in tools for omnigent.

Public API:
- ``LoadSkillTool``: Loads a skill's instructions by name.
- ``ReadSkillFileTool``: Reads files from a skill's directory.
- ``any_skill_has_resources``: Checks if any skill has bundled
  resource files (used by ToolManager to decide whether to
  register ReadSkillFileTool).
- ``list_skill_resources``: Lists resource files in a skill's
  directory (used by LoadSkillTool to append file listings).
- ``format_skill_content``: Formats a skill's content for the LLM,
  appending a resource file listing if present.
- ``find_skill_by_name``: Looks up a skill by exact name in a
  merged (bundled + host) skill list.
- ``format_skill_meta_text``: Builds the hidden ``<skill>`` wrapper
  text injected when a slash command invokes a skill (resolved on
  the runner, where ``skill_dir`` paths are valid).
- ``get_builtin_tool``: Instantiate a built-in tool by name.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from omnigent.kernel.pluggable.errors import ProviderNotRegistered
from omnigent.kernel.pluggable.registry import PluggableRegistry
from omnigent.spec.types import SkillSpec
from omnigent.tools.base import Tool
from omnigent.tools.builtins.agents import (
    SysAgentDownloadTool,
    SysAgentGetTool,
    SysAgentListTool,
)
from omnigent.tools.builtins.async_inbox import (
    SysCallAsyncTool,
    SysCancelAsyncTool,
    SysReadInboxTool,
)
from omnigent.tools.builtins.list_comments import ListCommentsTool
from omnigent.tools.builtins.list_models import SysListModelsTool
from omnigent.tools.builtins.load_skill import (
    LoadSkillTool,
    find_skill_by_name,
    format_skill_content,
    format_skill_meta_text,
    list_skill_resources,
)
from omnigent.tools.builtins.memory import (
    MemoryAppendTool,
    MemoryCompartmentsListTool,
    MemoryQueryTool,
)
from omnigent.tools.builtins.read_skill_file import (
    ReadSkillFileTool,
)
from omnigent.tools.builtins.skills import (
    SysSkillApplyTool,
    SysSkillInstalledTool,
    SysSkillRemoveTool,
    SysSkillResolveTargetsTool,
    SysSkillSearchTool,
    SysSkillSourcesTool,
    SysSkillStagePreviewTool,
)
from omnigent.tools.builtins.spawn import (
    SysSessionCloseTool,
    SysSessionCreateTool,
    SysSessionGetHistoryTool,
    SysSessionGetInfoTool,
    SysSessionListTool,
    SysSessionSendTool,
)
from omnigent.tools.builtins.timer import (
    SysTimerCancelTool,
    SysTimerSetTool,
)
from omnigent.tools.builtins.update_comment import UpdateCommentTool
from omnigent.tools.builtins.web_search import WebSearchTool

__all__ = [
    "BUILTIN_NAMES",
    "INSTANTIABLE_BUILTINS",
    "ListCommentsTool",
    "LoadSkillTool",
    "MemoryAppendTool",
    "MemoryCompartmentsListTool",
    "MemoryQueryTool",
    "ReadSkillFileTool",
    "SysAgentDownloadTool",
    "SysAgentGetTool",
    "SysAgentListTool",
    "SysCallAsyncTool",
    "SysCancelAsyncTool",
    "SysListModelsTool",
    "SysReadInboxTool",
    "SysSessionCloseTool",
    "SysSessionCreateTool",
    "SysSessionGetHistoryTool",
    "SysSessionGetInfoTool",
    "SysSessionListTool",
    "SysSessionSendTool",
    "SysSkillApplyTool",
    "SysSkillInstalledTool",
    "SysSkillRemoveTool",
    "SysSkillResolveTargetsTool",
    "SysSkillSearchTool",
    "SysSkillSourcesTool",
    "SysSkillStagePreviewTool",
    "SysTimerCancelTool",
    "SysTimerSetTool",
    "UpdateCommentTool",
    "WebSearchTool",
    "any_skill_has_resources",
    "find_skill_by_name",
    "format_skill_content",
    "format_skill_meta_text",
    "get_builtin_tool",
    "list_skill_resources",
    "register_extension_tools",
]

# Lazy imports avoid circular import cycles — each tool's actual
# class is imported only when the factory fires.

# Factory type: each constructor accepts a config dict and returns
# a Tool. Callable is used instead of type[Tool] because the base
# Tool.__init__ does not declare a config parameter — only the
# web search subclasses do.
_BuiltinFactory = Callable[[dict[str, str]], Tool]


def _create_upload_file(config: dict[str, str]) -> Tool:
    """
    Lazy factory for UploadFileTool.

    :param config: Tool config (unused).
    :returns: An UploadFileTool instance.
    """
    from omnigent.tools.builtins.upload_file import UploadFileTool

    return UploadFileTool()


def _create_search_conversations(config: dict[str, str]) -> Tool:
    """
    Lazy factory for SearchConversationsTool.

    :param config: Tool config (unused).
    :returns: A SearchConversationsTool instance.
    """
    from omnigent.tools.builtins.search_conversations import (
        SearchConversationsTool,
    )

    return SearchConversationsTool()


def _create_list_files(config: dict[str, str]) -> Tool:
    """
    Lazy factory for ListFilesTool.

    :param config: Tool config (unused).
    :returns: A ListFilesTool instance.
    """
    from omnigent.tools.builtins.list_files import ListFilesTool

    return ListFilesTool()


def _create_download_file(config: dict[str, str]) -> Tool:
    """
    Lazy factory for DownloadFileTool.

    :param config: Tool config (unused).
    :returns: A DownloadFileTool instance.
    """
    from omnigent.tools.builtins.download_file import DownloadFileTool

    return DownloadFileTool()


def _create_export_agent(config: dict[str, str]) -> Tool:
    """
    Lazy factory for ExportAgentTool.

    :param config: Tool config (unused).
    :returns: An ExportAgentTool instance.
    """
    from omnigent.tools.builtins.export_agent import ExportAgentTool

    return ExportAgentTool()


# Skill-acquisition tools (sys_skill_*, BDP-2487). Opt-in: only registered
# for agents that list them in ``tools.builtins`` (install is privileged).
# Schema-only — the runner dispatches them over server_client (see
# ``_SKILL_ACQ_TOOLS`` in omnigent.runner.tool_dispatch). One factory builds
# any of the seven from its (config-free) class.
def _create_skill_tool(config: dict[str, str], cls: type[Tool]) -> Tool:
    """
    Lazy factory for a schema-only ``sys_skill_*`` tool.

    :param config: Tool config (unused — these tools take no spec config).
    :param cls: The skill-tool class to instantiate.
    :returns: An instance of *cls*.
    """
    return cls()


# Framework-owned reserved names: these occupy the builtin name-space
# but are NEVER instantiated by a user spec directive, so they carry no
# factory. They are kept out of the :class:`PluggableRegistry` (whose
# factories must be callable) and folded into :data:`BUILTIN_NAMES`
# separately. ``web_fetch`` is constructed by ToolManager before reaching
# this registry; ``list_comments`` / ``update_comment`` are auto-registered
# by ``ToolManager._register_comment_tools``; ``sys_list_models`` by
# ``ToolManager._register_sub_agent_tools``. They are reserved here so user
# specs cannot shadow them. (Policy ASKs are surfaced as MCP-shape
# elicitations on the SSE stream — not via the tool registry — see
# omnigent/runtime/policies/approval.py.)
#
# Note: the legacy ``terminal_run`` / ``terminal_list`` /
# ``terminal_close`` / ``terminal_send_input`` family was deleted
# per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §3a + §6.2. Their
# replacement is the ``sys_terminal_*`` family registered
# automatically by ``ToolManager._register_terminal_tools`` when
# the spec declares a ``terminals:`` block — not via this
# registry. One-shot shell commands now use ``sys_os_shell``
# instead.
_FRAMEWORK_OWNED_NAMES: frozenset[str] = frozenset(
    {
        "web_fetch",
        "list_comments",
        "update_comment",
        "sys_list_models",
    }
)

# The first-party builtin tool set. Each entry is a config-accepting
# factory (the ``_BuiltinFactory`` shape). This is the default set this
# plugin contributes to the ``tools`` seam; it is the same mapping that
# used to be spelled inline in the old ``_BUILTIN_REGISTRY`` dict.
_FIRST_PARTY_FACTORIES: dict[str, _BuiltinFactory] = {
    # User-enablable tools (factory present).
    "web_search": lambda config: WebSearchTool(config=config),
    "upload_file": _create_upload_file,
    "list_files": _create_list_files,
    "download_file": _create_download_file,
    "search_conversations": _create_search_conversations,
    "export_agent": _create_export_agent,
    # Omnigent-native agent memory plane (FU1, ADR-0132). No spec config.
    "memory_append": lambda config: MemoryAppendTool(),
    "memory_query": lambda config: MemoryQueryTool(),
    "memory_compartments_list": lambda config: MemoryCompartmentsListTool(),
    # Skill-acquisition (sys_skill_*, BDP-2487). Opt-in install surface for the
    # Skills Concierge; runner-dispatched over server_client (require_user
    # mutating routes). Schema-only here; no spec config.
    "sys_skill_search": partial(_create_skill_tool, cls=SysSkillSearchTool),
    "sys_skill_sources": partial(_create_skill_tool, cls=SysSkillSourcesTool),
    "sys_skill_installed": partial(_create_skill_tool, cls=SysSkillInstalledTool),
    "sys_skill_resolve_targets": partial(_create_skill_tool, cls=SysSkillResolveTargetsTool),
    "sys_skill_stage_preview": partial(_create_skill_tool, cls=SysSkillStagePreviewTool),
    "sys_skill_apply": partial(_create_skill_tool, cls=SysSkillApplyTool),
    "sys_skill_remove": partial(_create_skill_tool, cls=SysSkillRemoveTool),
}


def _thunk(factory: _BuiltinFactory) -> Callable[[], _BuiltinFactory]:
    """Wrap a config-accepting *factory* as the zero-arg thunk the registry stores.

    :class:`PluggableRegistry` stores zero-arg factories (``Callable[[], T]``)
    and calls them on :meth:`~PluggableRegistry.get`, so every tool name is
    registered as a thunk that *returns* its ``_BuiltinFactory`` verbatim.
    ``get(name)`` thus yields the config-accepting factory, not a ``Tool``
    instance; :func:`get_builtin_tool` threads the spec ``config`` into it.

    :param factory: The config-accepting builtin/extension tool factory.
    :returns: A zero-arg thunk returning *factory* unchanged.
    """
    return lambda: factory


def _build_builtin_registry() -> PluggableRegistry[_BuiltinFactory]:
    """
    Build the ``tools`` seam registry for the **first-party** builtin tools.

    The seam is keyed by tool name and is generic over ``T =
    _BuiltinFactory`` — i.e. the *provider* a seam resolves to is the
    config-accepting builtin factory (a ``Callable[[dict], Tool]``), not a
    ``Tool`` instance. Each name is registered as a thunk (see :func:`_thunk`)
    that *returns* its ``_BuiltinFactory``; :func:`get_builtin_tool` then
    threads the spec ``config`` into that factory at instantiation time.
    Storing the factory (not an instance) preserves the lazy per-tool import
    deferral the old dict relied on — nothing imports until the tool is
    actually resolved.

    **Import-safe (BDP-2371 / BDP-2506).** This builds *only* the first-party
    set (web_search, upload_file, memory_*, sys_skill_*). It deliberately does
    NOT call ``discover_extensions()`` — entry-point discovery loads the
    FastAPI-heavy extension hub and must stay off the runner hot path, exactly
    as ``web_search._build_provider_registry`` keeps it off. Extension tools —
    e.g. the ByteDesk goals/peer/deliberation/outcome/signal/routing tools
    (ADR-0143 / BDP-2300) — are merged later by :func:`register_extension_tools`,
    called once at server startup. This restores the baseline timing, where the
    old ``**extension_tool_factories()`` splice was invisible to the import
    safety net because no entry points are installed on the runner hot path.

    :returns: A populated :class:`PluggableRegistry` keyed by first-party name.
    """
    registry: PluggableRegistry[_BuiltinFactory] = PluggableRegistry("tools")
    for name, factory in _FIRST_PARTY_FACTORIES.items():
        registry.register(name, _thunk(factory))
    return registry


# The ``tools`` seam: a PluggableRegistry keyed by tool name whose
# providers are config-accepting builtin/extension tool factories.
# Built once at module import with the first-party set only (same lifetime
# the old dict had). Extension tools are merged in at server startup by
# ``register_extension_tools`` — NOT at import (BDP-2371 / BDP-2506).
_BUILTIN_REGISTRY: PluggableRegistry[_BuiltinFactory] = _build_builtin_registry()


def _recompute_name_sets() -> None:
    """Refresh the module-level reserved-name frozensets from the live registry.

    :data:`BUILTIN_NAMES` and :data:`INSTANTIABLE_BUILTINS` are snapshots of the
    registry's current name set. They are computed once at import (first-party
    only) and recomputed by :func:`register_extension_tools` after the
    server-startup extension merge, so extension-contributed tool names become
    reserved (and instantiable) once discovery has run — matching the baseline,
    where the entry-point splice populated these sets in the server process.
    Consumers re-fetch the symbol (e.g. ``spec.validator`` imports it lazily),
    so the rebind is observed.
    """
    global BUILTIN_NAMES, INSTANTIABLE_BUILTINS
    BUILTIN_NAMES = frozenset(_BUILTIN_REGISTRY.names()) | _FRAMEWORK_OWNED_NAMES
    INSTANTIABLE_BUILTINS = frozenset(_BUILTIN_REGISTRY.names())


# Canonical set of every reserved builtin name: the instantiable
# seam names plus the framework-owned names that carry no factory.
# Single source of truth — no drift between the reserved-name check
# and the factory dispatch.
BUILTIN_NAMES: frozenset[str] = frozenset(_BUILTIN_REGISTRY.names()) | _FRAMEWORK_OWNED_NAMES

# Subset of names that have a user-facing factory. Used by the
# onboarding ``list_builtin_tools`` helper, which only lists
# tools an agent spec can actually enable via
# ``tools.builtins`` — framework-owned names would just confuse
# the agent author. Every seam-registered name is instantiable;
# framework-owned names are excluded by construction.
INSTANTIABLE_BUILTINS: frozenset[str] = frozenset(_BUILTIN_REGISTRY.names())


def register_extension_tools() -> None:
    """Merge extension-contributed builtin tools into the ``tools`` seam (startup).

    The companion to :func:`_build_builtin_registry`: it runs entry-point
    discovery and folds each discovered extension's ``tool_factories()`` hook
    into the module-level :data:`_BUILTIN_REGISTRY`, then refreshes
    :data:`BUILTIN_NAMES` / :data:`INSTANTIABLE_BUILTINS`. This is the SAME work
    the old ``**extension_tool_factories()`` splice did inline in the registry
    dict, but deferred to **server startup** so the FastAPI-heavy extension hub
    stays off the runner hot path (BDP-2371 / BDP-2506) — mirroring how
    ``web_search`` defers its provider discovery to ``discover_all_extensions``.

    Idempotent: re-running merely re-attempts registration, and an
    already-registered tool name is skipped by the registry's conflict guard
    (caught per-extension below). Error-isolated per extension so one bad
    extension can never break boot. Discovery is routed through
    :func:`omnigent.kernel.pluggable.registry.discover_extensions`, the lazy proxy that
    defers the heavy ``omnigent.kernel.extensions`` import.
    """
    import logging

    from omnigent.kernel.pluggable.errors import RegistryConflict
    from omnigent.kernel.pluggable.registry import discover_extensions

    logger = logging.getLogger(__name__)
    for ext in discover_extensions():
        getter = getattr(ext, "tool_factories", None)
        if getter is None:
            continue
        try:
            factories = dict(getter())
        except Exception:  # extensions are best-effort; never break boot
            logger.warning(
                "extension %r failed to contribute tool_factories for the "
                "tools seam",
                getattr(ext, "name", ext),
                exc_info=True,
            )
            continue
        for name, factory in factories.items():
            try:
                _BUILTIN_REGISTRY.register(name, _thunk(factory))
            except RegistryConflict:
                # Already registered (e.g. a re-run, or two extensions
                # contributing the same name) — keep the first; never break boot.
                logger.debug(
                    "tool %r already registered in the tools seam; skipping "
                    "the duplicate from extension %r",
                    name,
                    getattr(ext, "name", ext),
                )
    _recompute_name_sets()


def get_builtin_tool(
    name: str,
    config: dict[str, str] | None = None,
) -> Tool | None:
    """
    Instantiate a built-in tool by name with optional config.

    :param name: The tool name from ``tools.builtins`` in
        config.yaml, e.g. ``"web_search"``.
    :param config: Tool-specific key-value pairs from the spec,
        e.g. ``{"api_key": "sk-...", "engine_id": "abc"}``.
        ``None`` or empty dict means no spec-level config was
        provided.
    :returns: A :class:`Tool` instance, or ``None`` if the
        name is not recognized.
    """
    # Returns None for both "not in registry" AND
    # "framework-owned without factory" — callers treat both
    # as "not instantiable via this entry point". Check against
    # BUILTIN_NAMES first if you need to distinguish. ``get`` raises
    # ProviderNotRegistered for an unknown name (which includes the
    # framework-owned names, deliberately not seam-registered).
    try:
        factory = _BUILTIN_REGISTRY.get(name)
    except ProviderNotRegistered:
        return None
    return factory(config or {})


def any_skill_has_resources(
    skills: list[SkillSpec],
) -> bool:
    """
    Check whether any skill has bundled resource files.

    :param skills: The agent's skill list, e.g.
        ``[SkillSpec(name="code-review", ...)]``.
    :returns: ``True`` if at least one skill has a
        ``skill_dir`` with files in references/, scripts/,
        or assets/.
    """
    return any(list_skill_resources(s) for s in skills)
