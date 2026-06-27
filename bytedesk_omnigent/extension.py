"""The single ByteDesk extension (ADR-0143, BDP-2291 / BDP-2300).

Contributes ALL ByteDesk surfaces to omnigent core through the generic
``omnigent.kernel.extensions`` seam — routers, background lifespan loops, tool factories,
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
    from omnigent.stores.permission_store import PermissionStore
    from omnigent.tools.base import Tool

logger = logging.getLogger(__name__)

#: PG advisory-lock key for the boot-time tool-step resume sweep (BDP-2252).
_TOOL_STEP_RESUME_LOCK = 0x746F6F6C73746570

#: PG advisory-lock key for the boot-time workflow-orchestrator task seed (BDP-2337).
_WORKFLOW_TASK_SEED_LOCK = 0x776B666C77746B73

#: Tool-name prefix the ByteDesk extension claims for server-side execution
#: (the three-tier keyed ``memory__*`` tools — BDP-2458 / BDP-2505).
_MEMORY_TOOL_PREFIX = "memory__"


def _memory_tool_interceptor(
    tool_name: str,
    arguments: dict | None,
    *,
    caller_agent_id: str | None = None,
    caller_department: str | None = None,
) -> str | None:
    """Server-side handler for ``memory__*`` tool calls (BDP-2505).

    Contributed via :meth:`BytedeskExtension.tool_interceptors` under the
    ``memory__`` prefix. Returns the JSON result string when *tool_name* is a
    recognised memory op, or ``None`` to fall through to normal runner dispatch
    (keeping the prior ``is_memory_tool`` gate semantics — a ``memory__*`` name
    that is not a known op is NOT intercepted). The heavy memory imports stay
    deferred inside the body so merely registering the interceptor never pulls
    the memory stack onto the import path.
    """
    from bytedesk_omnigent.memory_tool_intercept import (
        execute_memory_tool,
        is_memory_tool,
    )

    if not is_memory_tool(tool_name):
        return None
    return execute_memory_tool(
        tool_name,
        arguments,
        caller_agent_id=caller_agent_id,
        caller_department=caller_department,
    )


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
    def routers(
        self,
        auth_provider: AuthProvider | None = None,
        permission_store: PermissionStore | None = None,
    ) -> list[APIRouter]:
        from bytedesk_omnigent.routes.agentic_inbox import create_agentic_inbox_router
        from bytedesk_omnigent.routes.config import create_config_router
        from bytedesk_omnigent.routes.goal_delivery import create_goal_delivery_router
        from bytedesk_omnigent.routes.goals import create_goals_router
        from bytedesk_omnigent.routes.governance import create_governance_router
        from bytedesk_omnigent.routes.inbound import create_inbound_router
        from bytedesk_omnigent.routes.ingress import create_ingress_router
        from bytedesk_omnigent.routes.integration_capabilities import (
            create_integration_capabilities_router,
        )
        from bytedesk_omnigent.routes.omni_cli_terminal import (
            create_omni_cli_terminal_router,
        )
        from bytedesk_omnigent.routes.skills_concierge import create_skills_concierge_router
        from bytedesk_omnigent.scheduler.router import create_schedules_router
        from bytedesk_omnigent.tasks.router import create_tasks_router

        return [
            _health_router(),
            create_governance_router(auth_provider=auth_provider),
            create_ingress_router(),
            create_goal_delivery_router(),
            create_inbound_router(auth_provider=auth_provider),
            create_agentic_inbox_router(),
            create_goals_router(
                auth_provider=auth_provider,
                permission_store=permission_store,
            ),
            create_skills_concierge_router(
                auth_provider=auth_provider,
                permission_store=permission_store,
            ),
            create_integration_capabilities_router(auth_provider=auth_provider),
            create_tasks_router(auth_provider=auth_provider),
            create_schedules_router(auth_provider=auth_provider),
            create_config_router(auth_provider=auth_provider),
            create_omni_cli_terminal_router(
                auth_provider=auth_provider,
                permission_store=permission_store,
            ),
        ]

    # ── default MCP servers (merged into EVERY agent spec, BDP-2459) ──
    def default_mcp_servers(self) -> list:
        """The shared-memory stdio MCP front, mounted on EVERY agent.

        A stdio server (``python -m bytedesk_omnigent.memory_mcp``) that ADVERTISES
        the ``memory__*`` tool schemas — one searchable + addressable shared-memory
        store across org/dept/agent. Execution is handled SERVER-SIDE at the
        ``tools/call`` choke point (``_handle_mcp_tools_call``), where the caller's
        verified identity is known (BDP-2458); the front carries the schemas only,
        not the identity, and never executes the tool bodies. ``env.PYTHONPATH=/build``
        so the spawned subprocess can import ``bytedesk_omnigent`` (the SDK's minimal
        stdio env omits PYTHONPATH; ``/build`` is the source mount in localDev and the
        install root in the prod image). Model sees ``memory__search`` /
        ``memory__get`` / ``memory__put`` / ``memory__append`` / ``memory__list`` /
        ``memory__unset``. An agent that declares its own ``memory`` server wins
        (merged by name in :func:`omnigent.spec.load`).
        """
        from omnigent.spec.types import MCPServerConfig

        return [
            MCPServerConfig(
                name="memory",
                transport="stdio",
                command="python",
                args=["-m", "bytedesk_omnigent.memory_mcp"],
                env={"PYTHONPATH": "/build"},
                tool_allowlist=["search", "get", "put", "append", "list", "unset"],
            )
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
            "bytedesk_omnigent.policies.google",
            "bytedesk_omnigent.policies.github",
        ]

    # ── builtin tool factories (merged into core _BUILTIN_REGISTRY) ───
    def tool_factories(self) -> dict[str, Callable[[object], Tool]]:
        from bytedesk_omnigent.tools.confluence_tools import BytedeskConfluenceTool
        from bytedesk_omnigent.tools.deliberation_tools import (
            DeliberationDecideTool,
            DeliberationFindTool,
            DeliberationPositionTool,
            DeliberationStartTool,
        )
        from bytedesk_omnigent.tools.github_tools import BytedeskGitHubTool
        from bytedesk_omnigent.tools.goal_tools import (
            GoalAdvanceTool,
            GoalClaimTool,
            GoalCreateTool,
            GoalDependencyUpdateTool,
            GoalListTool,
        )
        from bytedesk_omnigent.tools.image_generation_tools import BytedeskGenerateImageTool
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
        from bytedesk_omnigent.tools.slack_tools import BytedeskSlackTool

        return {
            "peer_send": lambda _c: PeerSendTool(),
            "peer_inbox": lambda _c: PeerInboxTool(),
            "goal_create": lambda _c: GoalCreateTool(),
            "goal_list": lambda _c: GoalListTool(),
            "goal_claim": lambda _c: GoalClaimTool(),
            "goal_advance": lambda _c: GoalAdvanceTool(),
            "goal_dependency_update": lambda _c: GoalDependencyUpdateTool(),
            "deliberation_start": lambda _c: DeliberationStartTool(),
            "deliberation_position": lambda _c: DeliberationPositionTool(),
            "deliberation_decide": lambda _c: DeliberationDecideTool(),
            "deliberation_find": lambda _c: DeliberationFindTool(),
            "outcome_record": lambda _c: OutcomeRecordTool(),
            "find_specialist": lambda _c: FindSpecialistTool(),
            "resolve_assignee": lambda _c: ResolveAssigneeTool(),
            "bytedesk_jira": lambda _c: BytedeskJiraTool(),
            "bytedesk_confluence": lambda _c: BytedeskConfluenceTool(),
            "bytedesk_github": lambda _c: BytedeskGitHubTool(),
            "bytedesk_slack": lambda _c: BytedeskSlackTool(),
            "bytedesk_generate_image": lambda _c: BytedeskGenerateImageTool(),
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
        """The ByteDesk gateway-header principal resolver, flag-gated.

        Prefers the **asymmetric** RSA verifier when ``OMNIGENT_ASSERTION_RSA_PUBLIC_KEY``
        is set (BDP-2424 — Office signs with the private key omnigent never holds,
        so omnigent verifies but cannot forge). Falls back to the HMAC secret
        ``OMNIGENT_BYTEDESK_PRINCIPAL_SECRET`` (BDP-2389). Returns ``[]`` when
        neither is configured, so a default deploy is zero behavior change — core
        does not even construct the composite chain.
        """
        from bytedesk_omnigent.auth.principal_resolver import (
            RSA_PUBLIC_KEY_ENV,
            SECRET_ENV,
            ByteDeskPrincipalResolver,
        )
        from omnigent.identity.verifiers import RsaAssertionVerifier

        rsa_pem = os.environ.get(RSA_PUBLIC_KEY_ENV, "").strip()
        if rsa_pem:
            return [ByteDeskPrincipalResolver(RsaAssertionVerifier.from_env(RSA_PUBLIC_KEY_ENV))]
        secret = os.environ.get(SECRET_ENV, "").strip()
        if secret:
            return [ByteDeskPrincipalResolver(secret)]
        return []

    # ── harness descriptors (HARNESS_REGISTRY seam, BDP-2507) ─────────
    def harness_descriptors(self) -> dict[str, Callable[[], object]]:
        """Contribute ByteDesk's ``hermes`` harness through the harness seam.

        This is the cross-package dogfooding path (Section 9.2): the ByteDesk
        Hermes Agent bridge (``bytedesk_omnigent.harnesses.hermes_native_harness``
        — Kade Vector's model-agnostic brain, ACP over ``hermes acp``) is declared
        here as a ``harness_descriptors`` contribution, exactly the way a third
        party would add a harness. Discovery runs at server startup via
        :func:`omnigent.kernel.pluggable.manifest.discover_all_extensions`.

        IDEMPOTENT: ``hermes`` is also present in core's first-party descriptor set
        so it is resolvable at *import* time (``_HARNESS_MODULES`` materializes
        before FastAPI-heavy extension discovery runs; importing this extension
        from the harness module would pull FastAPI onto the harness import hot
        path, BDP-2371). When core has already registered ``hermes`` this hook
        returns ``{}`` so the seam's conflict guard is never tripped; it only
        contributes the descriptor on the (future) path where core does not carry
        it. The descriptor factory imports nothing — ``module_path`` is a plain
        string the runner imports lazily — so this hook stays import-cheap.
        """
        from omnigent.runtime.harnesses.descriptors import (
            HARNESS_REGISTRY,
            HarnessDescriptor,
        )

        if "hermes" in HARNESS_REGISTRY.names():
            return {}
        return {
            "hermes": lambda: HarnessDescriptor(
                name="hermes",
                module_path="bytedesk_omnigent.harnesses.hermes_native_harness",
            )
        }

    # ── identity ports (adr-omnigent-pluggable-identity) ──────────────
    # ByteDesk's inbound trust uses the core HMAC/RSA verifier (via
    # ByteDeskPrincipalResolver) and authz keeps the core default. The OBO
    # token-exchange credential provider (BDP-2434) is the consumer layer for the
    # outbound seam — it only fires when an acting identity carries a
    # subject_token, else it returns None and the registry falls back to the core
    # static-secret default (degrade-to-default). The Office-backed JWKS verifier
    # / capability authorizer remain deferred (empty hooks below stay swappable).
    def assertion_verifiers(self) -> dict[str, Callable[[], object]]:
        return {}

    def outbound_credential_providers(self) -> dict[str, Callable[[], object]]:
        from bytedesk_omnigent.auth.obo_credential_provider import (
            OnBehalfOfCredentialProvider,
        )

        return {"token_exchange_obo": OnBehalfOfCredentialProvider}

    def authorization_providers(self) -> dict[str, Callable[[], object]]:
        return {}

    # ── server-side tool interception (ADR-0143 §5 Step 1, BDP-2505) ────
    def tool_interceptors(self) -> dict[str, Callable[..., object]]:
        """Claim the ``memory__*`` tool prefix for server-side execution.

        The three-tier keyed memory tools (``memory__*``) execute on the
        omnigent server itself, not on the runner: the server owns the memory
        store AND the verified caller identity, while the shared stdio memory
        front cannot carry a trustworthy per-agent identity (BDP-2458). This
        hook replaces the former hard ``from bytedesk_omnigent.memory_tool_intercept
        import ...`` in ``omnigent/server/routes/sessions.py`` — core now
        dispatches through the aggregated prefix table and never names
        ``bytedesk_omnigent`` (ADR-0143 §5 Step 1).

        The handler returns the JSON result string for a handled ``memory__*``
        op, or ``None`` to fall through to normal runner dispatch (e.g. a
        ``memory__*`` name that is not one of the recognised ops).
        """
        return {_MEMORY_TOOL_PREFIX: _memory_tool_interceptor}

    # ── config-control-plane descriptors (Settings Registry, ADR-0150) ─
    def config_descriptors(self) -> list:
        """ByteDesk's configurable properties for the ``/v1/config`` surface (BDP-2413)."""
        from bytedesk_omnigent.config import bytedesk_config_descriptors

        return bytedesk_config_descriptors()

    # ── background lifespan tasks (started + cancelled by the server) ─
    def background_tasks(self) -> list[Callable[[], Awaitable[None]]]:
        """The org background loops + the boot-time tool-step resume sweep. The
        server lifespan starts each as a task and cancels it on shutdown; the
        resume sweep is a one-shot that completes and returns (cancel is a no-op)."""
        return [
            self._configure_logging,
            self._signal_bus_reaper,
            self._inbound_retry_reaper,
            self._seed_inbound_flags,
            self._cron_scheduler,
            self._goal_engine,
            self._accountability,
            self._tool_step_resume,
            self._seed_workflow_tasks,
            self._realtime_bridge,
        ]

    async def _inbound_retry_reaper(self) -> None:
        from bytedesk_omnigent.inbound.reaper import inbound_retry_reaper_loop

        await inbound_retry_reaper_loop()

    async def _seed_inbound_flags(self) -> None:
        """One-shot: seed the inbound-pipeline feature flags (ADR-0155), default off."""
        from bytedesk_omnigent.inbound.flags import seed_inbound_flags

        try:
            await seed_inbound_flags()
        except Exception:  # noqa: BLE001 - seed must not block boot
            logger.warning("inbound flag seed skipped", exc_info=True)

    async def _configure_logging(self) -> None:
        """Surface the ``bytedesk_omnigent`` namespace's INFO logs. Core sets the
        ``omnigent`` namespace level in the lifespan AFTER uvicorn's dictConfig
        (omnigent/server/app.py), because a pre-dictConfig call is reset; the
        extension's loggers otherwise inherit root and stay silent (e.g. the
        BDP-2301 bridge-installed line never showed). background_tasks run in the
        same post-dictConfig lifespan window, so mirror core here — honouring the
        same OMNIGENT_LOG_LEVEL. One-shot: set the level and return."""
        level_name = os.environ.get("OMNIGENT_LOG_LEVEL", "INFO").upper()
        logging.getLogger("bytedesk_omnigent").setLevel(getattr(logging, level_name, logging.INFO))

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
        from bytedesk_omnigent.engine.cron import build_goal_cron_dispatch
        from bytedesk_omnigent.fabric.outbox import build_fabric_cron_dispatch
        from bytedesk_omnigent.scheduler import cron_scheduler_loop

        # Goal triggers (payload.kind == "goal") spawn a working session (BDP-2583);
        # every other trigger falls through to the fabric SQL outbox.
        logger.info("cron dispatch: goal-aware (goal → session, else fabric outbox)")
        dispatch = build_goal_cron_dispatch(build_fabric_cron_dispatch())
        await cron_scheduler_loop(dispatch=dispatch)

    async def _goal_engine(self) -> None:
        from bytedesk_omnigent.engine.loop import goal_engine_loop

        await goal_engine_loop()

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
                        logger.info("tool-step resume: reclaimed %d orphaned step(s)", reclaimed)
        except Exception as exc:  # noqa: BLE001 — boot sweep is best-effort
            logger.warning("tool-step resume sweep failed: %s", exc, exc_info=True)
