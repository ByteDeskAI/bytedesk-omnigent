"""First-party plugin for the ``omnigent.skills`` subpackage (BDP-2509).

Part of the microkernel / extension-author-SDK refactor (epic BDP-2503). This
module is the *dogfooding* registration glue described in
``docs/EXTENSION_FRAMEWORK_ANALYSIS.md`` Section 9 — it expresses this
subpackage as a first-party plugin that registers its **existing** default
providers through the same kernel seams a third-party extension would use,
with **no privileged hard-wiring**.

Per the Section 9.1 ``omnigent.skills`` row, this plugin contributes into two
kernel seams:

  * ``routers``        — the skill-acquisition route group
    (:func:`omnigent.server.routes.skills.create_skills_router`).
  * ``tool_factories`` — the seven schema-only ``sys_skill_*`` install tools
    (:mod:`omnigent.tools.builtins.skills`).

It is built with the :func:`omnigent.sdk.extension` decorator, so its instances
satisfy the kernel :class:`omnigent.extensions.OmnigentExtension` Protocol
(Section 12.7 invariant) and flow through the existing
``discover_extensions`` / ``install_extensions`` /
``PluggableRegistry.discover_extensions`` machinery unchanged — there is no
parallel discovery, lifecycle, or registry here.

**Boot wiring is deferred.** This plugin is *not* yet added to the host's
default plugin set; the Integration phase of BDP-2503 does that. This module
only needs to import cleanly and expose correctly-shaped hook returns. It moves
or rewrites **nothing** — the concrete providers stay where they live and are
imported lazily inside each hook body to remain circular-import-safe (the
kernel-keeps-domain-free + deferred-import invariant) and to keep the FastAPI /
domain stack off any hot import path.

The ``requires=("omnigent.spec",)`` hint mirrors the Section 9.1 boot-order
dependency (skill acquisition reads agent specs); it is a declarative hint the
kernel's dependency-ordering / ``assert_extension`` machinery can act on, not an
import-time coupling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.sdk import extension, router, tool

if TYPE_CHECKING:  # pragma: no cover — typing only, never imported at runtime
    from fastapi import APIRouter

    from omnigent.server.auth import AuthProvider
    from omnigent.tools.base import Tool


@extension(name="omnigent.skills", requires=("omnigent.spec",))
class SkillsExtension:
    """First-party plugin exposing the skills subpackage's default providers.

    Registers the *already-existing* skill-acquisition router and the seven
    ``sys_skill_*`` install tools through the kernel seams. Each hook defers its
    domain imports so importing this module stays kernel-light and free of the
    circular-import hazards documented in Section 6.
    """

    # ── routers seam ───────────────────────────────────────────────────────
    @router()
    def skills_router(self, auth_provider: AuthProvider | None = None) -> APIRouter:
        """Build the skill-acquisition route group.

        Wraps the existing
        :func:`omnigent.server.routes.skills.create_skills_router` factory. The
        stores it needs are resolved from the runtime accessors at install time
        (when the runtime is initialised and routes are being mounted); they are
        imported here, inside the hook body, to keep this module domain-free.

        :param auth_provider: Forwarded by the kernel ``routers()`` seam; the
            route group uses it to gate the mutating ``/skills/*`` routes.
        :returns: The skills :class:`fastapi.APIRouter`.
        """
        # Deferred / domain imports — kept inside the hook so importing this
        # plugin never drags in FastAPI, the routes module, or the runtime.
        from omnigent.runtime import (
            get_agent_cache,
            get_agent_store,
            get_artifact_store,
        )
        from omnigent.server.routes.skills import create_skills_router

        return create_skills_router(
            get_agent_store(),
            get_agent_cache(),
            get_artifact_store(),
            auth_provider=auth_provider,
        )

    # ── tool_factories seam (the seven sys_skill_* install tools) ──────────
    # Each method returns the existing concrete tool instance unchanged. The
    # classes are config-free (the kernel's per-tool config is ignored — mirror
    # of the existing ``_create_skill_tool`` factory), and imported lazily to
    # stay circular-import-safe with ``omnigent.tools.builtins``.
    @tool(name="sys_skill_search")
    def sys_skill_search(self) -> Tool:
        from omnigent.tools.builtins.skills import SysSkillSearchTool

        return SysSkillSearchTool()

    @tool(name="sys_skill_sources")
    def sys_skill_sources(self) -> Tool:
        from omnigent.tools.builtins.skills import SysSkillSourcesTool

        return SysSkillSourcesTool()

    @tool(name="sys_skill_installed")
    def sys_skill_installed(self) -> Tool:
        from omnigent.tools.builtins.skills import SysSkillInstalledTool

        return SysSkillInstalledTool()

    @tool(name="sys_skill_resolve_targets")
    def sys_skill_resolve_targets(self) -> Tool:
        from omnigent.tools.builtins.skills import SysSkillResolveTargetsTool

        return SysSkillResolveTargetsTool()

    @tool(name="sys_skill_stage_preview")
    def sys_skill_stage_preview(self) -> Tool:
        from omnigent.tools.builtins.skills import SysSkillStagePreviewTool

        return SysSkillStagePreviewTool()

    @tool(name="sys_skill_apply")
    def sys_skill_apply(self) -> Tool:
        from omnigent.tools.builtins.skills import SysSkillApplyTool

        return SysSkillApplyTool()

    @tool(name="sys_skill_remove")
    def sys_skill_remove(self) -> Tool:
        from omnigent.tools.builtins.skills import SysSkillRemoveTool

        return SysSkillRemoveTool()


__all__ = ["SkillsExtension"]
