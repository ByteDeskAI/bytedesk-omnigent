# Omnigent autonomous feature loop — iteration 18

## Capability shipped

Iteration 18 adds a deterministic integration replay-safety plan compiler for connected-app onboarding and operations.

New surface:

- `POST /v1/integration-replay-plans/compile`
- Pure compiler module: `bytedesk_omnigent.integration_replay_plans`
- Route module: `bytedesk_omnigent.routes.integration_replay_plans`

The compiler turns a third-party event descriptor into a previewable replay contract containing:

- Canonical provider slug.
- Stable idempotency key for event retries and provider writeback.
- Replay strategy (`dedupe_then_dispatch` or `dedupe_then_manual_review`).
- Risk level and approval requirement.
- Retry policy.
- Provider-scoped dead-letter queue recommendation.
- Deterministic harness steps inspired by Archon-style workflows: normalize event, dedupe event, verify binding, optional approval gate, dispatch agent, record receipt, and optional writeback.

## Prior loop awareness

Before selecting this capability, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #96 / iteration 1: integration capability catalog.
- #98 / iteration 2: external work-item intake.
- #99 / iteration 3: integration workflow plan compiler.
- #100 / iteration 4: connected-app manifest compiler.
- #101 / iteration 5: Slack webhook ingress adapter.
- #102 / iteration 6: Stripe webhook ingress adapter.
- #103 / iteration 7: GitHub webhook event routing.
- #104 / iteration 8: JSON payload webhook adapter.
- #105 / iteration 9: integration approval plan compiler.
- #106 / iteration 10: Microsoft Teams webhook ingress adapter.
- #107 / iteration 11: Linear webhook ingress adapter.
- #108 / iteration 12: Shopify webhook ingress adapter.
- #109 / iteration 13: webhook binding management API.
- #110 / iteration 14: integration event route compiler.
- #111 / iteration 15: integration secret readiness plans.
- #112 / iteration 16: integration OAuth state tokens.
- #113 / iteration 17: webhook ingress preflight preview.

This iteration avoids duplicating those open branches. It adds the missing replay-safety and operations contract that sits after event-route compilation/preflight and before production event delivery is trusted at scale.

## Implementation details

- `compile_integration_replay_plan()` accepts `provider`, `workspace_id`, `event_type`, `operation`, optional `external_id`, and optional `writeback`.
- The compiler normalizes provider/operation names and creates an idempotency key in the shape `integration-replay:v1:{provider}:{workspace_id}:{event_type}:{external_id}`.
- Customer/system-of-record write operations for providers such as HubSpot, Salesforce, Zendesk, Intercom, Stripe, and Shopify require manual review before replay.
- Collaboration fast paths such as Discord or Slack mentions remain low-risk and can dedupe then dispatch without an approval gate.
- The ByteDesk extension mounts the route under the existing `/v1` extension prefix so ByteDesk Platform can call it during connector setup or incident response.

## Business case

Connected-app integrations create value only when customers trust that autonomous agents will not double-fire, replay stale provider events, or silently lose events when provider webhooks retry. A deterministic replay plan gives ByteDesk Platform a simple artifact to show during installation and to use during support incidents:

1. Which event is safe to replay?
2. Which idempotency key prevents duplicate autonomous work?
3. Does replay require human approval because the action writes to a CRM, support desk, billing system, or commerce system?
4. Where should failed events be dead-lettered for later review?

This makes Omnigent safer to embed in third-party applications and more credible for enterprise integrations where auditability, retry behavior, and manual recovery are purchase requirements.

## Future unlocks

- Persist replay receipts so Platform can list event attempts and outcomes per connected app install.
- Attach compiled replay plans to webhook binding management and ingress preflight responses.
- Add provider-specific event families and risk overrides from the integration capability catalog once those branches land.
- Use the idempotency key as a shared correlation id across signal delivery, task creation, provider writeback, and dead-letter review.
- Build an operator UI for replaying dead-lettered events with approval context.

## Test plan

TDD was used: the initial targeted pytest run failed during collection because `bytedesk_omnigent.integration_replay_plans` did not exist yet.

Verified after implementation:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_replay_plans.py -q`
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_replay_plans.py bytedesk_omnigent/routes/integration_replay_plans.py bytedesk_omnigent/extension.py tests/bytedesk_omnigent/test_integration_replay_plans.py`
- `git diff --check`

Targeted scope only: this change adds one pure compiler, one thin FastAPI route, extension router registration, focused tests, and loop documentation. A full repository test suite was not run because the surgical surface is small and the targeted pytest/lint/diff checks cover the changed behavior.
