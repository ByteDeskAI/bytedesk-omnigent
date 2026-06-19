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

        return [
            _health_router(),
            create_governance_router(auth_provider=auth_provider),
            create_ingress_router(),
            create_goals_router(auth_provider=auth_provider),
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
        from bytedesk_omnigent.tools.outcome_tools import OutcomeRecordTool
        from bytedesk_omnigent.tools.peer_tools import PeerInboxTool, PeerSendTool
        from bytedesk_omnigent.tools.routing_tools import FindSpecialistTool
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
            "signal_await": lambda _c: SignalAwaitTool(),
            "signal_deliver": lambda _c: SignalDeliverTool(),
            "signal_check": lambda _c: SignalCheckTool(),
        }

    # ── secret backends (consulted by omnigent.onboarding.secrets) ───
    def secret_backends(self) -> list:
        """Infisical as the default secret store (BDP-2303); inert without creds."""
        from bytedesk_omnigent.secrets.infisical import InfisicalBackend

        return [InfisicalBackend()]

    # ── background lifespan tasks (started + cancelled by the server) ─
    def background_tasks(self) -> list[Callable[[], Awaitable[None]]]:
        """The org background loops + the boot-time tool-step resume sweep. The
        server lifespan starts each as a task and cancels it on shutdown; the
        resume sweep is a one-shot that completes and returns (cancel is a no-op)."""
        return [
            self._signal_bus_reaper,
            self._cron_scheduler,
            self._accountability,
            self._tool_step_resume,
        ]

    async def _signal_bus_reaper(self) -> None:
        from bytedesk_omnigent.bus.reaper import signal_bus_reaper_loop

        await signal_bus_reaper_loop()

    async def _cron_scheduler(self) -> None:
        from bytedesk_omnigent.scheduler import cron_scheduler_loop
        from bytedesk_omnigent.sessions import (
            build_cron_dispatch,
            get_session_initiator,
        )

        initiator = get_session_initiator()
        dispatch = build_cron_dispatch(initiator) if initiator is not None else None
        await cron_scheduler_loop(dispatch=dispatch)

    async def _accountability(self) -> None:
        from bytedesk_omnigent.accountability import accountability_loop

        await accountability_loop(
            manager_agent_id=os.getenv("OMNIGENT_ACCOUNTABILITY_MANAGER") or None
        )

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
