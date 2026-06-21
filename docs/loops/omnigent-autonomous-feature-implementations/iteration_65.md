# Autonomous feature loop iteration 65 — integration cutover checklist compiler

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_65`

## Capability shipped

Iteration 65 adds a deterministic integration cutover checklist compiler for the canonical integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/cutover-checklist`

New internal compiler:

- `bytedesk_omnigent.integration_cutover_checklist.compile_integration_cutover_checklist(slug)`

Given a catalog slug such as `slack-command-center`, `github-engineering-copilot`, or `archon-style-workflow-blueprints`, the compiler returns a secret-free, JSON-ready activation runbook with:

- capability identity, category, auth model, required scopes, and risk tier;
- required approval roles derived from risk tier;
- verification gate ids and evidence count inherited from the verification matrix;
- six deterministic cutover phases: catalog freeze, credential boundary, dry-run rehearsal, limited production window, evidence review, and rollback-or-scale;
- phase owners, entry criteria, and exit evidence suitable for ByteDesk Platform UI or an autonomous operator loop.

## Prior loop awareness

Before selecting this capability, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- the integration capability catalog and `/v1/integration-capabilities` endpoint;
- connected app manifests, workflow plans, handoff packages, task briefs, event routes, workflow harnesses, approval gates, activation gates, replay/rollback/rate-limit/dead-letter/retry/idempotency/backfill compilers, OAuth/credential helpers, event envelopes, contract fingerprints, gap analysis, verification matrices, marketplace listings, staffing plans, demo scenarios, readiness assessments, dependency graphs, and risk registers;
- provider-specific webhook ingress adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry.

This iteration deliberately does not add another provider adapter and does not duplicate the open readiness/risk/dependency work. It adds the next missing operator primitive: a deterministic cutover runbook that turns existing readiness evidence into a step-by-step activation decision path.

## Implementation details

Added:

- `bytedesk_omnigent/integration_cutover_checklist.py`
  - `CutoverPhase`: immutable phase model with JSON-ready serialization.
  - `compile_integration_cutover_checklist(slug)`: looks up a catalog capability, reuses the existing verification matrix, derives risk-tier approvals, and emits an ordered activation checklist.
  - Risk-tier approval rules:
    - `internal_harness`: `integration_owner`
    - `external_read`: `tenant_admin`, `integration_owner`
    - `external_write`: `tenant_admin`, `security_owner`, `integration_owner`
  - Harness-specific phase labeling/ownership for Archon-style deterministic workflow blueprints.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `GET /integration-capabilities/{slug}/cutover-checklist` under the existing authenticated/read-only catalog router.
  - Unknown slugs return the same `not_found` error shape used by sibling catalog endpoints.
- `omnigent/server/API.md`
  - Documents the new endpoint and response shape.

Added tests:

- `tests/bytedesk_omnigent/test_integration_cutover_checklist.py`
  - verifies provider cutover phases, approvals, inherited catalog/verification evidence, and gate ids;
  - verifies the internal harness path uses a lighter approval boundary and workflow rehearsal wording;
  - verifies unknown slug behavior;
  - verifies the HTTP route and 404 behavior.

## Business case

Omnigent is accumulating connector planning primitives: catalog entries, readiness assessments, verification matrices, risk registers, and dependency graphs. The moment that matters to customers is still cutover: deciding whether a connector or deterministic workflow harness can be safely activated for a tenant.

This feature gives ByteDesk Platform and autonomous operators a consistent activation runbook:

- product can show a clear "what happens next" plan after a capability is readiness-approved;
- security and tenant admins can see exactly which approval roles are required before external reads or writes;
- autonomous builders can rehearse provider ingress, idempotency, approvals, evidence review, and rollback before enabling customer-facing automation;
- marketplace integration reviews can standardize activation decisions across OAuth providers and internal workflow harnesses.

## Future unlocks

1. Store per-tenant cutover checklist state so ByteDesk Platform can render phase progress and blockers.
2. Link satisfied verification-matrix evidence directly into each phase's entry criteria.
3. Generate customer-facing activation packets from the checklist for Slack, GitHub, Google Workspace, and CRM/support connectors.
4. Feed cutover completion into policy gates so high-risk external writes remain disabled until approvals and evidence are recorded.
5. Combine this checklist with the open risk register and dependency graph work to produce a complete go/no-go dashboard.

## Test plan

TDD red run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_cutover_checklist.py -q
```

Initial result: expected collection failure because `bytedesk_omnigent.integration_cutover_checklist` did not exist yet.

Targeted green run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_cutover_checklist.py -q
```

Result: `4 passed, 1 warning in 0.15s`.

Additional regression scope:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_cutover_checklist.py -q
```

Result: `14 passed, 1 warning in 0.20s`.

Targeted lint:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_cutover_checklist.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_cutover_checklist.py
```

Result: `All checks passed!`.

Whitespace check:

```bash
git diff --check
```

Result: passed with no output.

The pytest warning is the repository's existing `tests/known_failures.yaml` collection warning and is not introduced by this feature.
