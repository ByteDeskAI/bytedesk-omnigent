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

    # Jira is shared-secret (not HMAC); register its adapter so resolve_webhook_adapter
    # returns it for source="jira" instead of the GitHub HMAC default.
    register_webhook_adapter("jira", JiraWebhookAdapter)

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

        projector = GoalDeliveryProjector(get_goal_store())
        if source == "github":
            event = parse_github_pr_event(payload)
            if event is None:
                return JSONResponse(
                    {"status": "ignored", "detail": "not a merged pull_request"},
                    status_code=202,
                )
            result = projector.apply_github_pr_merged(event)
        elif source == "jira":
            wid = request.headers.get("x-atlassian-webhook-identifier")
            event = parse_jira_issue_event(payload, webhook_identifier=wid)
            if event is None:
                return JSONResponse(
                    {"status": "ignored", "detail": "no issue in payload"},
                    status_code=202,
                )
            result = projector.apply_jira_issue_updated(event)
        else:
            return JSONResponse(
                {"status": "unsupported_source", "detail": source}, status_code=404
            )

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
