# Autonomous feature loop iteration 62 — integration readiness assessment

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_62`

## Capability shipped

Iteration 62 adds a deterministic integration readiness assessment compiler for the canonical ByteDesk Omnigent integration capability catalog.

New API surface:

- `POST /v1/integration-capabilities/{slug}/readiness-assessment`

New Python API:

- `compile_integration_readiness_assessment(slug: str, evidence=...)`
- `GateReadinessAssessment`

The compiler scores caller-submitted rollout evidence against the existing verification matrix for any catalog capability. It returns:

- capability identity and inherited risk tier;
- activation state (`ready`, `in_progress`, or `blocked_by_policy_evidence`);
- readiness percentage;
- satisfied/total gate counts;
- satisfied/missing evidence counts;
- the next missing gate id;
- per-gate status plus satisfied and missing evidence.

The implementation is pure and deterministic: it does not call third-party services, read credentials, inspect GitHub, or persist tenant data.

## Prior loop awareness

Before selecting this feature, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers provider webhook adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry. It also covers the integration catalog, gap analysis, verification matrices, marketplace listings, staffing plans, demo scenarios, OAuth/scope helpers, activation gates, approval/replay/rollback/rate-limit/dead-letter/retry/idempotency/backfill compilers, event envelopes, contract fingerprints, and workflow-harness planning.

This iteration deliberately does not add another connector, marketplace package, staffing plan, demo script, or verification checklist. It implements the next missing operational primitive from iteration 58's future unlocks: taking evidence for a verification matrix and turning it into a concrete readiness score that ByteDesk Platform or an autonomous rollout harness can display before enabling an integration.

## Implementation details

Added:

- `bytedesk_omnigent/integration_readiness_assessment.py`
  - `GateReadinessAssessment`: immutable per-gate status model with JSON-ready serialization.
  - `compile_integration_readiness_assessment(...)`: scores submitted evidence by exact matrix gate id and required-evidence label.
  - deterministic activation-state logic:
    - all gates satisfied -> `ready`;
    - external-write capability without a satisfied policy gate -> `blocked_by_policy_evidence`;
    - otherwise -> `in_progress`.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `POST /integration-capabilities/{slug}/readiness-assessment` under the existing authenticated/read-only catalog router.
  - Unknown slugs return the existing catalog-style `404` JSON shape.
  - The request body accepts an `evidence` object keyed by verification gate id, with each value being evidence strings matching the matrix's `required_evidence` entries.

Added tests:

- `tests/bytedesk_omnigent/test_integration_readiness_assessment.py`
  - verifies partial evidence scoring for Slack and external-write policy blocking;
  - verifies a fully evidenced Archon-style workflow harness reaches `ready`;
  - verifies unknown slug behavior;
  - verifies the HTTP route response and 404 behavior.

## Business case

Omnigent is moving from cataloging integrations toward safely activating autonomous agents inside third-party systems. The business risk is not just whether a connector exists; it is whether operators can prove the connector is scoped, auditable, replay-safe, policy-gated, observable, and rollback-ready before customer activation.

This readiness assessment gives ByteDesk Platform a customer-facing and operator-facing progress contract:

1. Tenant admins can see why an integration is not yet ready instead of receiving a vague disabled state.
2. Autonomous rollout agents can prioritize the next missing evidence gate deterministically.
3. High-risk external-write connectors are blocked until policy evidence exists, reducing unsafe automation risk.
4. Marketplace and onboarding surfaces can show quantified readiness without live credentials or provider calls.
5. Archon-style workflow harnesses can treat readiness scoring as a deterministic terminal gate before enabling repeatable workflows.

## Future unlocks

1. Store per-tenant readiness evidence in an audit ledger and feed it into this compiler.
2. Render readiness progress bars in ByteDesk Platform integration marketplace and onboarding flows.
3. Combine readiness assessments with iteration 57 gap analysis so next recommended integrations include both priority and activation blockers.
4. Attach readiness scoring to deterministic workflow-harness certification runs.
5. Require `ready` state before allowing high-risk external-write connector activation.

## Test plan

TDD red phase:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_readiness_assessment.py -q
```

Initial result: expected collection failure because `bytedesk_omnigent.integration_readiness_assessment` did not exist yet.

Targeted green run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_readiness_assessment.py -q
```

Result: `4 passed, 1 warning in 0.14s`.

Additional regression scope:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_readiness_assessment.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_capabilities.py -q
```

Result: `14 passed, 1 warning in 0.18s`.

Targeted lint:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_readiness_assessment.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_readiness_assessment.py
```

Result: `All checks passed!`.

Whitespace check:

```bash
git diff --check
```

Result: passed with no output.

The pytest warning is the repository's existing `tests/known_failures.yaml` collection warning and is unrelated to this feature.
