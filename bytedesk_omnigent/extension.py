"""The single ByteDesk extension (ADR-0143, BDP-2291 / BDP-2300).

Contributes ALL ByteDesk surfaces to omnigent core through the generic
``omnigent.extensions`` seam — routers, background lifespan loops, tool factories,
and policy modules — so core carries no ByteDesk-specific registration glue
(Phase 5: zero ByteDesk conflicts on upstream rebase).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:
    from omnigent.server.auth import AuthProvider
    from omnigent.tools.base import Tool

logger = logging.getLogger(__name__)

#: PG advisory-lock key for the boot-time tool-step resume sweep (BDP-2252).
_TOOL_STEP_RESUME_LOCK = 0x746F6F6C73746570

#: PG advisory-lock key for the boot-time workflow-orchestrator task seed (BDP-2337).
_WORKFLOW_TASK_SEED_LOCK = 0x776B666C77746B73


def _health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/_ext/health")
    async def ext_health() -> dict:
        return {"extension": "bytedesk", "loaded": True}

    return router


class BytedeskExtension:
    """ByteDesk's omnigent extension (ADR-0143). Owns all ByteDesk contributions."""

    name = "bytedesk"

    # ── routers ──────────────────────────────────────────────────────
    def routers(self, auth_provider: AuthProvider | None = None) -> list[APIRouter]:
        from bytedesk_omnigent.routes.goals import create_goals_router
        from bytedesk_omnigent.routes.governance import create_governance_router
        from bytedesk_omnigent.routes.ingress import create_ingress_router
        from bytedesk_omnigent.routes.integration_capabilities import (
            create_integration_capabilities_router,
        )
        from bytedesk_omnigent.tasks.router import create_tasks_router

        return [
            _health_router(),
            create_governance_router(auth_provider=auth_provider),
            create_ingress_router(),
            create_goals_router(auth_provider=auth_provider),
            create_integration_capabilities_router(auth_provider=auth_provider),
            create_tasks_router(auth_provider=auth_provider),
        ]

    # ── policy modules (scanned by the policy registry) ──────────────
    def policy_modules(self) -> list[str]:
        return [
            "bytedesk_omnigent.policies.verify_gate",
            "bytedesk_omnigent.policies.spawn_governor",
            "bytedesk_omnigent.policies.budget",
            "bytedesk_omnigent.policies.forever_gate",
            "bytedesk_omnigent.policies.two_key",
            "bytedesk_omnigent.policies.dry_run",
            "bytedesk_omnigent.policies.delegation",
            "bytedesk_omnigent.policies.outreach_compliance",
        ]

    # ── builtin tool factories (merged into core _BUILTIN_REGISTRY) ───
    def tool_factories(self) -> dict[str, Callable[[object], Tool]]:
        from bytedesk_omnigent.tools.deliberation_tools import (
            DeliberationDecideTool,
            DeliberationFindTool,
            DeliberationPositionTool,
            DeliberationStartTool,
        )
        from bytedesk_omnigent.tools.goal_tools import (
            GoalAdvanceTool,
            GoalClaimTool,
            GoalCreateTool,
            GoalListTool,
        )
        from bytedesk_omnigent.tools.jira_tools import BytedeskJiraTool
        from bytedesk_omnigent.tools.outcome_tools import OutcomeRecordTool
        from bytedesk_omnigent.tools.peer_tools import PeerInboxTool, PeerSendTool
        from bytedesk_omnigent.tools.routing_tools import (
            FindSpecialistTool,
            ResolveAssigneeTool,
        )
        from bytedesk_omnigent.tools.signal_tools import (
            SignalAwaitTool,
            SignalCheckTool,
            SignalDeliverTool,
        )

        return {
            "peer_send": lambda _c: PeerSendTool(),
            "peer_inbox": lambda _c: PeerInboxTool(),
            "goal_create": lambda _c: GoalCreateTool(),
            "goal_list": lambda _c: GoalListTool(),
            "goal_claim": lambda _c: GoalClaimTool(),
            "goal_advance": lambda _c: GoalAdvanceTool(),
            "deliberation_start": lambda _c: DeliberationStartTool(),
            "deliberation_position": lambda _c: DeliberationPositionTool(),
            "deliberation_decide": lambda _c: DeliberationDecideTool(),
            "deliberation_find": lambda _c: DeliberationFindTool(),
            "outcome_record": lambda _c: OutcomeRecordTool(),
            "find_specialist": lambda _c: FindSpecialistTool(),
            "resolve_assignee": lambda _c: ResolveAssigneeTool(),
            "bytedesk_jira": lambda _c: BytedeskJiraTool(),
            "signal_await": lambda _c: SignalAwaitTool(),
            "signal_deliver": lambda _c: SignalDeliverTool(),
            "signal_check": lambda _c: SignalCheckTool(),
        }

    # ── secret backends (consulted by omnigent.onboarding.secrets) ───
    def secret_backends(self) -> list:
        """Infisical as the default secret store (BDP-2303); inert without creds."""
        from bytedesk_omnigent.secrets.infisical import InfisicalBackend

        return [InfisicalBackend()]

    def principal_resolvers(self) -> list:
        """The ByteDesk gateway-header principal resolver, flag-gated (BDP-2389).

        Registers :class:`~bytedesk_omnigent.auth.principal_resolver.ByteDeskPrincipalResolver`
        ONLY when the signing secret ``OMNIGENT_BYTEDESK_PRINCIPAL_SECRET`` is
        set. With no secret it returns ``[]`` so a default deploy is zero
        behavior change — core does not even construct the composite chain.
        """
        from bytedesk_omnigent.auth.principal_resolver import (
            SECRET_ENV,
            ByteDeskPrincipalResolver,
        )

        secret = os.environ.get(SECRET_ENV, "").strip()
        if not secret:
            return []
        return [ByteDeskPrincipalResolver(secret)]

    # ── background lifespan tasks (started + cancelled by the server) ─
    def background_tasks(self) -> list[Callable[[], Awaitable[None]]]:
        """The org background loops + the boot-time tool-step resume sweep. The
        server lifespan starts each as a task and cancels it on shutdown; the
        resume sweep is a one-shot that completes and returns (cancel is a no-op)."""
        return [
            self._configure_logging,
            self._signal_bus_reaper,
            self._cron_scheduler,
            self._accountability,
            self._tool_step_resume,
            self._seed_workflow_tasks,
            self._realtime_bridge,
        ]

    async def _configure_logging(self) -> None:
        """Surface the ``bytedesk_omnigent`` namespace's INFO logs. Core sets the
        ``omnigent`` namespace level in the lifespan AFTER uvicorn's dictConfig
        (omnigent/server/app.py), because a pre-dictConfig call is reset; the
        extension's loggers otherwise inherit root and stay silent (e.g. the
        BDP-2301 bridge-installed line never showed). background_tasks run in the
        same post-dictConfig lifespan window, so mirror core here — honouring the
        same OMNIGENT_LOG_LEVEL. One-shot: set the level and return."""
        level_name = os.environ.get("OMNIGENT_LOG_LEVEL", "INFO").upper()
        logging.getLogger("bytedesk_omnigent").setLevel(
            getattr(logging, level_name, logging.INFO)
        )

    async def _realtime_bridge(self) -> None:
        """Install the office:agents roster bridge (BDP-2301). One-shot: wraps the
        agent store + returns. Runs in lifespan, i.e. AFTER the construction-time
        builtin-agent re-seed, so the ~74 seed creates are not emitted (no
        roster.changed storm on cold start) — only post-boot mutations fan out."""
        from bytedesk_omnigent.realtime import install_realtime_bridge

        install_realtime_bridge()

    async def _signal_bus_reaper(self) -> None:
        from bytedesk_omnigent.bus.reaper import signal_bus_reaper_loop

        await signal_bus_reaper_loop()

    async def _cron_scheduler(self) -> None:
        from bytedesk_omnigent.scheduler import cron_scheduler_loop
        from bytedesk_omnigent.sessions import (
            build_cron_dispatch,
            build_self_call_initiator_from_env,
            get_session_initiator,
            set_session_initiator,
        )

        # Register the live session initiator (BDP-2347): without it the cron
        # clock fires but dispatches nothing (the silent log-only no-op). The
        # self-call initiator re-enters the runtime via the trusted sessions HTTP
        # route — the same registry also backs run_task's dispatch. Honour an
        # already-registered initiator (tests / a future in-process one); else
        # build one from the env, which fail-closes to None when unconfigured.
        initiator = get_session_initiator()
        if initiator is None:
            initiator = build_self_call_initiator_from_env()
            if initiator is not None:
                set_session_initiator(initiator)
                logger.info("cron dispatch: live SessionInitiator registered")
            else:
                logger.warning(
                    "cron dispatch: no SessionInitiator configured (set %s) — "
                    "scheduled triggers will log only, not dispatch",
                    "OMNIGENT_SELF_BASE_URL",
                )

        dispatch = build_cron_dispatch(initiator) if initiator is not None else None
        await cron_scheduler_loop(dispatch=dispatch)

    async def _accountability(self) -> None:
        from bytedesk_omnigent.accountability import accountability_loop

        await accountability_loop(
            manager_agent_id=os.getenv("OMNIGENT_ACCOUNTABILITY_MANAGER") or None
        )

    async def _seed_workflow_tasks(self) -> None:
        """Seed the workflow orchestrators as first-class Tasks (BDP-2337). ADDITIVE:
        the workflow agents stay in the roster verbatim; this only adds derived Task
        rows from the same ``OMNIGENT_BUILTIN_AGENT_DIRS`` bundles. One-shot,
        PG-advisory-locked so only one pod seeds; idempotent so a re-run is a no-op."""
        from bytedesk_omnigent.tasks import get_task_store
        from bytedesk_omnigent.tasks.seed import seed_workflow_tasks
        from omnigent.runtime.memory_maintenance import advisory_lock

        try:
            store = get_task_store()
            with advisory_lock(store.engine, _WORKFLOW_TASK_SEED_LOCK) as acquired:
                if acquired:
                    count = await asyncio.to_thread(seed_workflow_tasks, store=store)
                    logger.info(
                        "workflow-task seed: %d workflow orchestrator task(s) present",
                        count,
                    )
        except Exception as exc:  # noqa: BLE001 — boot seed is best-effort
            logger.warning("workflow-task seed failed: %s", exc, exc_info=True)

    async def _tool_step_resume(self) -> None:
        from bytedesk_omnigent.runtime import get_tool_step_store
        from omnigent.runtime.memory_maintenance import advisory_lock

        try:
            store = get_tool_step_store()
            with advisory_lock(store.engine, _TOOL_STEP_RESUME_LOCK) as acquired:
                if acquired:
                    reclaimed = await asyncio.to_thread(store.resume_stale)
                    if reclaimed:
                        logger.info(
                            "tool-step resume: reclaimed %d orphaned step(s)", reclaimed
                        )
        except Exception as exc:  # noqa: BLE001 — boot sweep is best-effort
            logger.warning("tool-step resume sweep failed: %s", exc, exc_info=True)
