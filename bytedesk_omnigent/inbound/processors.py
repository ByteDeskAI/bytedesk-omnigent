"""Inbound processors — the fan-out consumers (ADR-0155, BDP-2561).

Each processor is an **Observer** with a **Message Filter** ``interested()`` predicate;
the registry is the **Content-Based Router**. The three are thin **shims** that
delegate to the already-tested consumer functions (GoalDeliveryProjector,
signal-bus deliver, agentic-inbox process_email_event) — no business logic is
rewritten here. One processor per event ``type`` (no cross fan-out yet, ADR-0155 risk #3).
"""

from __future__ import annotations

from collections.abc import Callable

from omnigent.kernel.pluggable import PluggableRegistry

from bytedesk_omnigent.inbound.event import InboundEvent
from bytedesk_omnigent.inbound.pipeline import InboundProcessor, ProcessorOutcome


class GoalDeliveryProcessor:
    """Project GitHub PR-merged / Jira issue-updated onto goal milestones (ADR-0154)."""

    name = "goal-delivery"

    def interested(self, event: InboundEvent) -> bool:
        return event.type in ("pull_request.merged", "jira.issue_updated")

    def handle(self, event: InboundEvent) -> ProcessorOutcome:
        from bytedesk_omnigent.goals import get_goal_store
        from bytedesk_omnigent.goals_delivery import (
            GithubPrEvent,
            GoalDeliveryProjector,
            JiraIssueEvent,
        )

        n = event.normalized
        projector = GoalDeliveryProjector(get_goal_store())
        if event.type == "pull_request.merged":
            result = projector.apply_github_pr_merged(
                GithubPrEvent(
                    repo=n["repo"],
                    pr_number=n["prNumber"],
                    head_ref=n["headRef"],
                    base_ref=n["baseRef"],
                    merge_commit_sha=n.get("mergeCommitSha"),
                )
            )
        else:
            result = projector.apply_jira_issue_updated(
                JiraIssueEvent(
                    issue_key=n["issueKey"],
                    issue_type=n["issueType"],
                    status=n["status"],
                    status_category=n["statusCategory"],
                    parent_epic_key=n.get("parentEpicKey"),
                    webhook_identifier=event.event_id,
                )
            )
        if result.matched:
            return ProcessorOutcome(status="ok", http_status=202, detail=result.detail)
        return ProcessorOutcome(status="skipped", http_status=404, detail=result.detail)


class SignalBusProcessor:
    """Deliver a signal to a parked session (ADR-0142 ingress path)."""

    name = "signal-bus"

    def interested(self, event: InboundEvent) -> bool:
        return event.type == "signal.deliver"

    def handle(self, event: InboundEvent) -> ProcessorOutcome:
        from bytedesk_omnigent.bus.signal_bus import DeliveryStatus
        from bytedesk_omnigent.ingress import get_binding_store
        from bytedesk_omnigent.runtime import get_signal_bus

        match_key = event.normalized.get("matchKey", "*")
        binding = get_binding_store().resolve_binding(source=event.source, match_key=match_key)
        if binding is None:
            return ProcessorOutcome(status="skipped", http_status=404, detail="no binding")
        result = get_signal_bus().deliver(signal_id=binding.signal_id, payload=event.raw_payload)
        if result.status is DeliveryStatus.DELIVERED:
            return ProcessorOutcome(status="ok", http_status=202)
        if result.status is DeliveryStatus.ALREADY_RESOLVED:
            return ProcessorOutcome(status="skipped", http_status=409, detail="already resolved")
        if result.status is DeliveryStatus.EXPIRED:
            return ProcessorOutcome(status="skipped", http_status=410, detail="wait expired")
        return ProcessorOutcome(status="skipped", http_status=404, detail="no pending wait")


class AgenticInboxProcessor:
    """Dispatch an inbound email to the mailbox's agent (BDP-2455)."""

    name = "agentic-inbox"

    def interested(self, event: InboundEvent) -> bool:
        return event.type == "email.received"

    def handle(self, event: InboundEvent) -> ProcessorOutcome:
        from bytedesk_omnigent.agentic_inbox import (
            AgenticInboxEmailEvent,
            AgenticInboxEventStatus,
            AgenticInboxResolver,
            get_agentic_inbox_event_store,
            process_email_event,
        )
        from bytedesk_omnigent.sessions import (
            build_self_call_initiator_from_env,
            get_session_initiator,
            set_session_initiator,
        )
        from omnigent.runtime import get_agent_cache, get_agent_store

        email_event = AgenticInboxEmailEvent.from_payload(event.raw_payload)
        initiator = get_session_initiator()
        if initiator is None:
            initiator = build_self_call_initiator_from_env()
            if initiator is not None:
                set_session_initiator(initiator)
        if initiator is None:
            return ProcessorOutcome(
                status="failed", http_status=503, detail="no SessionInitiator", retryable=True
            )
        resolver = AgenticInboxResolver(get_agent_store(), get_agent_cache())
        result = process_email_event(
            email_event,
            store=get_agentic_inbox_event_store(),
            resolve_agent_id=resolver.resolve_agent_id,
            initiator=initiator,
        )
        if result.status is AgenticInboxEventStatus.DISPATCHED:
            return ProcessorOutcome(status="ok", http_status=202, detail=result.detail)
        if result.status is AgenticInboxEventStatus.FAILED:
            return ProcessorOutcome(
                status="failed", http_status=503, detail=result.detail, retryable=True
            )
        # DUPLICATE / DEAD_LETTERED — handled, not a retryable failure
        return ProcessorOutcome(status="skipped", http_status=202, detail=result.detail)


def _build_processor_registry() -> PluggableRegistry[InboundProcessor]:
    """The inbound-processor registry (no default — fan-out, not single-select)."""
    registry: PluggableRegistry[InboundProcessor] = PluggableRegistry("inbound_processor")
    registry.register("goal-delivery", GoalDeliveryProcessor)
    registry.register("signal-bus", SignalBusProcessor)
    registry.register("agentic-inbox", AgenticInboxProcessor)
    return registry


_processor_registry: PluggableRegistry[InboundProcessor] | None = None


def register_processor(name: str, factory: Callable[[], InboundProcessor]) -> None:
    """Register an inbound processor *factory* (idempotent)."""
    global _processor_registry
    if _processor_registry is None:
        _processor_registry = _build_processor_registry()
    if name not in _processor_registry.names():
        _processor_registry.register(name, factory)


def all_processors() -> list[InboundProcessor]:
    """All registered processors (instances). The route passes these into ``ingest``;
    the pipeline's per-event ``interested()`` filter is the Content-Based Router."""
    global _processor_registry
    if _processor_registry is None:
        _processor_registry = _build_processor_registry()
    return [_processor_registry.get(name) for name in _processor_registry.names()]


def interested_processors(event: InboundEvent) -> list[InboundProcessor]:
    """Processors whose Message-Filter predicate matches *event*."""
    return [p for p in all_processors() if p.interested(event)]
