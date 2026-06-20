# Iteration 34 — Integration rollback plan compiler

## Capability shipped

Added a deterministic third-party integration rollback plan compiler for Omnigent and ByteDesk Platform:

- Pure compiler: `bytedesk_omnigent.integration_rollback.compile_integration_rollback_plan`
- Read-only API route mounted through the ByteDesk extension: `GET /v1/integration-rollback-plan`
- Tests for deterministic provider-aware plans, generic unknown-provider safety, and route serialization

The compiler produces a stable, audit-ready compensation contract before an autonomous agent mutates an external SaaS object. It returns the required pre-mutation snapshot fields, verification evidence, ordered rollback steps, approval posture, and stable idempotency key. It performs no network calls, reads no secrets, and mutates no state.

## Prior loop awareness

Before selecting this work, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop PRs already cover:

- iteration 1: integration capability catalog
- iteration 2: external work item intake
- iteration 3: integration workflow plan compiler
- iteration 4: connected app manifest compiler
- iterations 5-8: Slack, Stripe, GitHub, and JSON payload webhook ingress surfaces
- iterations 9-22: approval, routing, binding, secret readiness, OAuth state, replay, handoff, activation, authorize URL, and deterministic workflow harness work
- iterations 23-33: Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, adapter manifest, GitLab, declarative HMAC, and Google Workspace ingress adapter work

This iteration avoids duplicating those surfaces. It does not add another webhook adapter, OAuth helper, activation gate, replay compiler, handoff package, or workflow harness. Instead it fills a safety gap that sits just before and after external mutations: deterministic rollback/compensation planning.

## Implementation details

### `bytedesk_omnigent.integration_rollback`

New frozen dataclasses:

- `IntegrationRollbackStep`
- `IntegrationRollbackPlan`

New public function:

- `compile_integration_rollback_plan(provider, operation, agent_id, external_ref, mutation_summary="", risk_level="medium")`

The compiler normalizes provider and operation names, computes a stable SHA-256-derived plan id, emits an `integration-rollback:<provider>:<digest>` idempotency key, and returns five fixed compensation steps:

1. `capture_pre_mutation_snapshot`
2. `freeze_followup_automation`
3. `apply_compensation`
4. `verify_external_state`
5. `publish_handoff_receipt`

Provider-aware snapshot and verification defaults currently cover common integration targets:

- GitHub
- Jira
- Linear
- Slack
- Notion
- HubSpot
- Salesforce
- Zendesk
- Google Workspace

Unknown providers intentionally receive a generic safe contract (`external_ref`, `before_state`, `changed_fields`) rather than invented service-specific semantics.

### `bytedesk_omnigent.routes.integration_rollback`

Adds:

- `GET /v1/integration-rollback-plan?provider=&operation=&agent_id=&external_ref=&mutation_summary=&risk_level=`

The route returns only deterministic JSON. It is suitable for ByteDesk Platform previews, approval cards, and dry-run UX because it does not call external APIs or inspect credentials.

### Extension registration

`BytedeskExtension.routers()` now includes `create_integration_rollback_router()`, so the endpoint mounts alongside existing ByteDesk extension surfaces.

## Business case

Omnigent's mission is not only to let agents act in third-party tools, but to make those actions safe enough for real customer workflows. Every meaningful integration eventually mutates external state: closing issues, updating CRM contacts, changing ticket statuses, posting messages, assigning tasks, or modifying documents.

Without a deterministic rollback contract, operators cannot confidently answer:

- What did the agent need to snapshot before writing?
- How can we compensate if the write was wrong?
- Which automation should be quieted to avoid a rollback-triggered loop?
- What evidence proves the external object is restored?
- Which idempotency key prevents repeated compensation writes?
- What handoff receipt should humans and downstream agents review?

This capability gives ByteDesk Platform a stable safety primitive for customer-facing integration approvals and autonomous agent governance.

## Future unlocks

- Feed rollback plans into the open approval, activation, workflow harness, replay, and handoff surfaces once those loop PRs land.
- Persist rollback plan ids and evidence on task/tool-step records so compensation is resumable after agent or pod restarts.
- Add provider defaults from the integration capability catalog once `/v1/integration-capabilities` lands on `develop`.
- Add UI cards that show snapshot requirements, approval gate, and verification evidence before external writes.
- Connect idempotency keys to webhook/event-route dispatch so external writes and compensating writes are replay-safe by default.

## Test plan

Targeted RED/GREEN tests run from the iteration 34 worktree:

```bash
# RED: failed before production code because bytedesk_omnigent.integration_rollback did not exist.
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_rollback_plan.py -q

# GREEN: pure compiler and route tests pass.
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_rollback_plan.py -q
```

Additional verification before PR:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_rollback.py bytedesk_omnigent/routes/integration_rollback.py bytedesk_omnigent/extension.py tests/test_integration_rollback_plan.py
git diff --check
```

The full suite was not run because this is a surgical, pure compiler plus read-only route change. The targeted test covers deterministic compilation, unknown-provider safety, and extension route mounting. Pytest emits the repository's existing `tests/known_failures.yaml` unmatched-entry warning; it is unrelated to this change.
