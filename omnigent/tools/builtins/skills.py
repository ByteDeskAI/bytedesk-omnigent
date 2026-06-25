"""Skill-acquisition tools (``sys_skill_*``) for the Skills Concierge (BDP-2487).

The orchestrator surface for discovering and **installing** agent skills. These
tools are **runner-dispatched**: the runner has no in-process skill store, so
each proxies one of the Omnigent server's existing ``/v1/skills/*`` routes (or
``/v1/agents`` for scope resolution) over ``server_client`` — the same channel
and security posture as the ``sys_agent_*`` family in
:mod:`omnigent.tools.builtins.agents`.

Why this replaces the old ``skills`` stdio MCP front: the user-gated mutating
routes (``previews`` / ``apply``) are ``require_user``-gated. The stdio front ran
in the runner with no server credential, so those routes 401'd. The runner's
``server_client`` already carries ``X-Omnigent-Runner-Tunnel-Token``, so
``RunnerTokenAuthProvider`` resolves the session owner and ``require_user``
passes. These ship as schema-only :class:`~omnigent.tools.base.Tool` subclasses —
the base-class ``invoke`` fails loud if the AP-side path ever reaches them.

These are **opt-in** builtins: an agent only gets them by listing the names in
``tools.builtins`` (install is privileged — not every agent should have it). The
Skills Concierge bundle lists all seven.

Surface (tool → route):

- ``sys_skill_search``         → ``POST /v1/skills/search``
- ``sys_skill_sources``        → ``GET  /v1/skills/sources``
- ``sys_skill_installed``      → ``GET  /v1/skills/installed?agent_id=``
- ``sys_skill_resolve_targets``→ ``GET  /v1/agents`` + scope filter
- ``sys_skill_stage_preview``  → ``POST /v1/skills/previews`` (install)
- ``sys_skill_apply``          → ``POST /v1/skills/previews/{id}/apply``
- ``sys_skill_remove``         → ``POST /v1/skills/previews`` (remove) then apply
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSkillSearchTool(Tool):
    """Search for installable agent skills online (skills.sh registry + npm).

    Returns ranked hits; each result's ``name`` is the ``owner/repo@skill``
    install ref to pass as ``source_ref`` to ``sys_skill_stage_preview``.
    Runner-dispatched (``POST /v1/skills/search``).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_skill_search"``."""
        return "sys_skill_search"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Search for installable agent skills online (skills.sh registry "
            "+ npm). Returns {results, errors}; each result's `name` is the "
            "owner/repo@skill install ref — pass it as source_ref to "
            "sys_skill_stage_preview."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function schema; ``query`` required."""
        return {
            "type": "function",
            "function": {
                "name": SysSkillSearchTool.name(),
                "description": SysSkillSearchTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What capability to search for, e.g. 'pdf export'.",
                        },
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional source filter, e.g. ['skills'].",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max hits to return (default 20).",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }


class SysSkillSourcesTool(Tool):
    """List the available skill sources and whether each is currently usable.

    Runner-dispatched (``GET /v1/skills/sources``).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_skill_sources"``."""
        return "sys_skill_sources"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List the available skill sources and whether each is currently "
            "usable. Returns {sources: [...]}."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function schema; no parameters."""
        return {
            "type": "function",
            "function": {
                "name": SysSkillSourcesTool.name(),
                "description": SysSkillSourcesTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }


class SysSkillInstalledTool(Tool):
    """List skills already installed (optionally for one agent).

    Runner-dispatched (``GET /v1/skills/installed?agent_id=``).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_skill_installed"``."""
        return "sys_skill_installed"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List skills already installed. Pass agent_id to scope to one "
            "agent. Returns {installed: [...]}."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function schema; ``agent_id`` optional."""
        return {
            "type": "function",
            "function": {
                "name": SysSkillInstalledTool.name(),
                "description": SysSkillInstalledTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Optional target agent id to scope the listing to.",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }


class SysSkillResolveTargetsTool(Tool):
    """Resolve a scope phrase to the concrete target agents to install into.

    ``scope`` is ``organization`` | ``department:<name>`` |
    ``employee:<id-or-display-name>`` | a bare agent display name.
    Workflow/orchestrator agents are excluded from org/department scopes.
    Runner-dispatched (``GET /v1/agents`` + scope filter).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_skill_resolve_targets"``."""
        return "sys_skill_resolve_targets"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Resolve a scope phrase to concrete target agents to install "
            "into. scope = 'organization' | 'department:<name>' | "
            "'employee:<id-or-name>' | a bare agent display name. "
            "Workflow/orchestrator agents are excluded from org/department "
            "scopes. Returns {targets: [{id, display_name, department}]} — "
            "pass the ids to sys_skill_stage_preview."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function schema; ``scope`` required."""
        return {
            "type": "function",
            "function": {
                "name": SysSkillResolveTargetsTool.name(),
                "description": SysSkillResolveTargetsTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scope": {
                            "type": "string",
                            "description": (
                                "Scope phrase: 'organization', "
                                "'department:<name>', 'employee:<id-or-name>', "
                                "or a bare agent display name."
                            ),
                        },
                    },
                    "required": ["scope"],
                    "additionalProperties": False,
                },
            },
        }


class SysSkillStagePreviewTool(Tool):
    """Stage (but do NOT apply) a skill install — fetch + validate + plan.

    Computes the per-agent actions and returns a preview to confirm before
    apply. ``install_mode`` defaults to ``skip_existing`` (idempotent re-run);
    use ``replace`` only for an explicit reinstall. Runner-dispatched
    (``POST /v1/skills/previews`` with ``operation: install``).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_skill_stage_preview"``."""
        return "sys_skill_stage_preview"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Stage (do NOT apply) a skill install: fetch + validate the skill "
            "files and compute per-agent actions, returning a preview to "
            "confirm before applying. install_mode defaults to skip_existing "
            "(idempotent); use 'replace' only for an explicit reinstall. "
            "Returns {preview_id, skills, target_actions}."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function schema; source/ref/targets required."""
        return {
            "type": "function",
            "function": {
                "name": SysSkillStagePreviewTool.name(),
                "description": SysSkillStagePreviewTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "Skill source, e.g. 'skills'.",
                        },
                        "source_ref": {
                            "type": "string",
                            "description": (
                                "The owner/repo@skill install ref from a "
                                "sys_skill_search result's `name`."
                            ),
                        },
                        "target_agent_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Agent ids to install into (from resolve_targets).",
                        },
                        "install_mode": {
                            "type": "string",
                            "description": "'skip_existing' (default) or 'replace'.",
                        },
                    },
                    "required": ["source", "source_ref", "target_agent_ids"],
                    "additionalProperties": False,
                },
            },
        }


class SysSkillApplyTool(Tool):
    """Apply a staged preview, persisting the skill into each target's bundle.

    ``agent_ids`` optionally narrows the apply to a subset of the preview's
    targets. Runner-dispatched (``POST /v1/skills/previews/{id}/apply``).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_skill_apply"``."""
        return "sys_skill_apply"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Apply a staged preview, persisting the skill into each target's "
            "bundle. Pass agent_ids to narrow to a subset of the preview's "
            "targets. Returns {results: [{agent_id, status, version, error}]}."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function schema; ``preview_id`` required."""
        return {
            "type": "function",
            "function": {
                "name": SysSkillApplyTool.name(),
                "description": SysSkillApplyTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "preview_id": {
                            "type": "string",
                            "description": "The preview_id from sys_skill_stage_preview.",
                        },
                        "agent_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional subset of the preview's targets to apply to.",
                        },
                    },
                    "required": ["preview_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysSkillRemoveTool(Tool):
    """Uninstall a skill from the given targets — the rollback primitive.

    Stages a ``remove`` preview for the skill then applies it. Runner-dispatched
    (``POST /v1/skills/previews`` with ``operation: remove``, then apply).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_skill_remove"``."""
        return "sys_skill_remove"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Uninstall a skill from the given targets — the rollback "
            "primitive. Stages a remove preview for skill_name then applies "
            "it. Returns {results: [...]} (same shape as sys_skill_apply)."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function schema; skill_name + targets required."""
        return {
            "type": "function",
            "function": {
                "name": SysSkillRemoveTool.name(),
                "description": SysSkillRemoveTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "The installed skill's name to uninstall.",
                        },
                        "target_agent_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Agent ids to uninstall the skill from.",
                        },
                    },
                    "required": ["skill_name", "target_agent_ids"],
                    "additionalProperties": False,
                },
            },
        }
