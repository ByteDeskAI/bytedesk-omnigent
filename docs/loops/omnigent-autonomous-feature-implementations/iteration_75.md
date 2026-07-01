# Autonomous feature loop iteration 75 — integration pilot plan compiler

## Capability shipped

Iteration 75 adds a deterministic integration pilot plan compiler for the canonical integration capability catalog:

- `bytedesk_omnigent.integration_pilot_plans.compile_integration_pilot_plan(slug)`
- `GET /v1/integration-capabilities/{slug}/pilot-plan`

The compiler converts a catalog capability plus its verification matrix risk tier into a tenant-safe first rollout plan. It returns a JSON-ready plan with:

- pilot tier (`internal_harness`, `external_read`, or `external_write`);
- pilot boundaries that constrain credentials, tenants, and provider mutations;
- recommended stakeholders for the pilot decision;
- success metrics matched to the integration category;
- exit criteria before GA or broader tenant activation.

## Prior loop awareness

Before choosing this capability, I inspected the open autonomous loop PRs whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open work already covers many provider adapters and rollout compilers, including Slack/Stripe/GitHub/Teams/Linear/Jira/HubSpot/Zendesk/Intercom/Shopify/Notion/Bitbucket/Sentry adapters and planning surfaces for approval, OAuth, secrets, activation gates, replay, rollback, rate limits, dead-letter handling, retry schedules, idempotency, backfill, contract fingerprints, gap analysis, verification matrices, readiness assessments, cutover checklists, sandbox fixtures, consent manifests, autonomy policies, incident drills, recommendation compilation, evidence packets, and tenant routing manifests.

This iteration avoids duplicating those. Instead of adding another adapter or readiness/gap surface, it adds the missing bridge from verification gates to a concrete first pilot rollout that ByteDesk Platform and autonomous planning agents can show to operators before enabling an integration for a tenant.

## Implementation details

- Added `bytedesk_omnigent/integration_pilot_plans.py`.
  - Defines `IntegrationPilotPlan` and `IntegrationPilotTier`.
  - Uses the existing catalog lookup plus `compile_integration_verification_matrix()` so pilot tiers stay aligned with the verification matrix risk classification.
  - Produces stricter boundaries for `external_write` integrations, including sandbox-only pilots and explicit operator approval for outbound writes.
  - Gives Archon-style workflow harnesses an internal dry-run plan that does not require external tenant credentials.
- Extended `bytedesk_omnigent/routes/integration_capabilities.py`.
  - Adds `GET /integration-capabilities/{slug}/pilot-plan` under the existing authenticated/read-only catalog router.
  - Returns the same not-found envelope as sibling catalog subresources.
- Added `tests/bytedesk_omnigent/test_integration_pilot_plans.py`.
  - Verifies internal harness pilot plans for `archon-style-workflow-blueprints`.
  - Verifies external-write pilot boundaries for `slack-command-center`.
  - Verifies unknown capabilities return no plan and the HTTP route returns 404.

## Business case

Omnigent is accumulating many integration capabilities in parallel. Customers and operators need more than a catalog entry or an acceptance checklist: they need a deterministic answer to “how do we safely try this with one tenant first?”

Pilot plans improve sales, customer success, and platform operations by making first activation concrete:

- reduces risk for OAuth and webhook integrations by constraining tenant scope and mutations;
- gives ByteDesk Platform a product-ready payload for an integration activation UI;
- gives autonomous planning loops a deterministic rollout plan they can attach to generated work orders;
- makes internal Archon-style workflow harnesses testable without external credentials, speeding template marketplace iteration.

## Future unlocks

1. Combine pilot plans with the open tenant routing manifest work so a platform operator can generate a tenant-specific activation preview.
2. Attach pilot plan completion evidence to the integration evidence packets once those PRs land.
3. Let the integration recommendation compiler include the pilot plan for the next recommended capability.
4. Add UI affordances in ByteDesk Platform for pilot tier, boundaries, stakeholders, and exit criteria.
5. Promote completed pilot plans into reusable marketplace launch templates for agent developers.

## Test plan

TDD red run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_pilot_plans.py -q
```

Initial result: expected collection failure because `bytedesk_omnigent.integration_pilot_plans` did not exist yet.

Green run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_pilot_plans.py -q
```

Result: `5 passed, 1 warning`.

Related regression run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_pilot_plans.py -q
```

Lint/diff checks:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_pilot_plans.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_pilot_plans.py
git diff --check
```
