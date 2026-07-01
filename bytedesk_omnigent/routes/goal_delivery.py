"""Goal-delivery webhook ingress (ADR-0154, BDP-2543/2544).

``POST /v1/goal-delivery/{source}`` for ``source`` in ``github`` / ``jira`` —
verify the signature (reusing the per-source webhook adapter + secret resolver
from ``bytedesk_omnigent.ingress``), normalize the body, and project it onto
goal/milestone state via :class:`GoalDeliveryProjector`. Distinct from the
signal-bus ``/v1/ingress/{source}`` route (ADR-0142): that wakes parked sessions;
this advances the durable goals backlog. Unmatched delivery events return 404
(never 2xx, BDP-1419); non-actionable events (PR not merged) are acknowledged
``ignored``. The route is thin glue over the tested projector + parsers.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def create_goal_delivery_router() -> APIRouter:
    """Build the goal-delivery webhook ingress router (ADR-0154)."""
    from bytedesk_omnigent.ingress import JiraWebhookAdapter, register_webhook_adapter
    from omnigent.kernel.pluggable.errors import RegistryConflict

    # Jira is shared-secret (not HMAC); register its adapter so resolve_webhook_adapter
    # returns it for source="jira" instead of the GitHub HMAC default. Idempotent:
    # building the router more than once (app rebuild / tests) must not raise — the
    # adapter is already registered, which is the desired end state.
    try:
        register_webhook_adapter("jira", JiraWebhookAdapter)
    except RegistryConflict:
        pass

    router = APIRouter()

    @router.post("/goal-delivery/{source}")
    async def receive(source: str, request: Request) -> JSONResponse:
        from bytedesk_omnigent.goals import get_goal_store
        from bytedesk_omnigent.goals_delivery import (
            GoalDeliveryProjector,
            parse_github_pr_event,
            parse_jira_issue_event,
        )
        from bytedesk_omnigent.ingress import resolve_secret, resolve_webhook_adapter

        secret = resolve_secret(source)
        if secret is None:
            return JSONResponse(
                {"status": "unknown_source", "detail": f"no secret configured for {source}"},
                status_code=404,
            )
        raw = await request.body()
        adapter = resolve_webhook_adapter(source)
        headers: dict[str, str] = dict(request.headers)
        # Jira can't set custom headers — bridge its ?secret= URL query into the
        # X-Omnigent-Secret header the JiraWebhookAdapter checks.
        if source == "jira" and "x-omnigent-secret" not in {k.lower() for k in headers}:
            query_secret = request.query_params.get("secret")
            if query_secret:
                headers["x-omnigent-secret"] = query_secret
        if not adapter.verify(raw, headers, secret):
            return JSONResponse({"status": "bad_signature"}, status_code=401)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse({"status": "bad_payload"}, status_code=400)

        # Strangler cutover (ADR-0155, BDP-2565): when the flag is on, run the
        # verified payload through the generic inbound pipeline instead of the
        # legacy projector path below. Default OFF → fall through unchanged. The
        # signature check above stays the auth boundary (the pipeline has none).
        from bytedesk_omnigent.inbound.flags import (
            INBOUND_CUTOVER_GOAL_DELIVERY,
            evaluate_inbound_flag,
        )

        if await evaluate_inbound_flag(INBOUND_CUTOVER_GOAL_DELIVERY, source=source):
            from bytedesk_omnigent.inbound.pipeline import ingest
            from bytedesk_omnigent.inbound.processors import all_processors
            from bytedesk_omnigent.inbound.store import get_inbound_event_store
            from bytedesk_omnigent.inbound.translators import CHANNEL_GOAL_DELIVERY

            result = ingest(
                channel=CHANNEL_GOAL_DELIVERY,
                source=source,
                raw_payload=payload,
                headers=headers,
                store=get_inbound_event_store(),
                processors=all_processors(),
            )
            return JSONResponse(
                {
                    "status": result.status,
                    "idempotencyKey": result.idempotency_key,
                    "eventType": result.event_type,
                    "duplicate": result.duplicate,
                    "detail": result.detail,
                },
                status_code=result.http_status,
            )

        from bytedesk_omnigent.idempotency import get_idempotency_store

        projector = GoalDeliveryProjector(get_goal_store())
        dedup_key: str | None = None
        if source == "github":
            event = parse_github_pr_event(payload)
            if event is None:
                return JSONResponse(
                    {"status": "ignored", "detail": "not a merged pull_request"},
                    status_code=202,
                )
            dedup_key = event.merge_commit_sha
            apply = lambda: projector.apply_github_pr_merged(event)  # noqa: E731
        elif source == "jira":
            wid = request.headers.get("x-atlassian-webhook-identifier")
            event = parse_jira_issue_event(payload, webhook_identifier=wid)
            if event is None:
                return JSONResponse(
                    {"status": "ignored", "detail": "no issue in payload"},
                    status_code=202,
                )
            dedup_key = wid
            apply = lambda: projector.apply_jira_issue_updated(event)  # noqa: E731
        else:
            return JSONResponse(
                {"status": "unsupported_source", "detail": source}, status_code=404
            )

        # Idempotent receipt (ADR-0009): a redelivery of an already-applied event is
        # skipped; an unmatched (404) event is NOT claimed so the source can retry
        # once its goal exists (BDP-1419). Concurrency is handled by the projector's
        # atomic milestone RMW (BDP-2553), so the check-then-claim TOCTOU is benign.
        # A missing dedup key (no merge_commit_sha / webhook id) just skips dedup.
        idem = get_idempotency_store() if dedup_key else None
        scope = f"goal-delivery:{source}"
        if idem is not None and dedup_key is not None and idem.is_claimed(scope=scope, key=dedup_key):
            return JSONResponse(
                {"status": "duplicate", "detail": "already processed"}, status_code=200
            )
        result = apply()
        if idem is not None and dedup_key is not None and result.matched:
            idem.claim(scope=scope, key=dedup_key)

        return JSONResponse(
            {
                "status": "projected" if result.matched else "no_match",
                "goalId": result.goal_id,
                "milestoneKey": result.milestone_key,
                "milestoneStatus": result.milestone_status,
                "milestoneCompleted": result.milestone_completed,
                "goalCompleted": result.goal_completed,
                "detail": result.detail,
            },
            status_code=result.http_status,
        )

    return router
