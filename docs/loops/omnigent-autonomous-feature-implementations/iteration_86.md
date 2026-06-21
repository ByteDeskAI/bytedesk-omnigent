# Autonomous feature loop iteration 86 — integration deprecation plan compiler

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_86`

## Capability shipped

Iteration 86 adds a deterministic integration deprecation plan compiler for the canonical integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/deprecation-plan`

New internal compiler:

- `bytedesk_omnigent.integration_deprecation_plan.compile_integration_deprecation_plan(slug)`

Given a catalog slug such as `slack-command-center`, `github-engineering-copilot`, or `archon-style-workflow-blueprints`, the compiler returns a JSON-ready retirement plan with:

- capability identity, category, and derived risk tier;
- customer notice requirements derived from risk tier;
- ordered retirement phases for announcement/freeze, ingress draining, mutation disablement, evidence archival, credential revocation, and successor finalization;
- phase owners and exit criteria that operators and autonomous loops can verify;
- category-specific retention notes so evidence survives connector shutdown;
- successor requirements that keep active work from disappearing during decommissioning.

## Prior loop awareness

Before selecting this capability, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- integration capability catalog and `/v1/integration-capabilities` endpoint;
- connected app manifests, workflow plans, handoff packages, task briefs, event routes, workflow harnesses, approval gates, activation gates, replay/rollback/rate-limit/dead-letter/retry/idempotency/backfill compilers, OAuth/credential helpers, event envelopes, contract fingerprints, gap analysis, verification matrices, and many rollout/readiness/evidence planning surfaces;
- provider-specific webhook ingress adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry.

This iteration deliberately does not add another provider adapter and does not duplicate activation, rollback, remediation, readiness, acceptance, or evidence-assessment work. It adds the missing lifecycle endpoint for planned connector retirement: safe deprecation after an integration is no longer wanted, has been superseded, or must be shut down for a tenant without losing operational/audit history.

## Implementation details

Added:

- `bytedesk_omnigent/integration_deprecation_plan.py`
  - `DeprecationPhase`: immutable phase model with JSON-ready serialization.
  - Shared retirement phases for freeze, drain, disable, archive, revoke, and finalize.
  - Category-specific retention notes for communication, project-management, knowledge, developer, CRM/support, commerce/billing, and workflow-harness integrations.
  - Notice windows derived from the existing verification matrix risk tier: 0 days for internal harnesses, 7 days for external read integrations, and 14 days for external write integrations.
  - `compile_integration_deprecation_plan(slug)`, which returns `None` for unknown catalog slugs and otherwise returns a deterministic dict.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `GET /integration-capabilities/{slug}/deprecation-plan` under the existing authenticated/read-only catalog router.
  - Unknown slugs return the same `not_found` error shape used by the existing detail and verification-matrix endpoints.

Added tests:

- `tests/bytedesk_omnigent/test_integration_deprecation_plan.py`
  - verifies external-write Slack retirement plans, customer notice, phases, retention notes, and reversibility boundary;
  - verifies internal Archon-style workflow harness retirement plans and successor requirements;
  - verifies unknown slug behavior;
  - verifies the HTTP route for GitHub engineering copilot and 404 behavior.

## Business case

Omnigent is becoming an integration middleware layer for autonomous agents. Customer trust depends not only on adding connectors quickly, but also on retiring connectors safely when providers change APIs, tenants churn, permissions are reduced, or a better workflow replaces an old one.

The deprecation plan compiler gives ByteDesk Platform and autonomous operators a consistent shutdown contract:

- customers can see when they need notice before an integration stops receiving events or mutating external systems;
- operators can drain work queues and preserve evidence before revoking credentials;
- security can distinguish the reversible phase from the credential-revocation point of no return;
- autonomous loops can generate retirement tasks without live provider credentials or tenant data;
- ByteDesk Platform can expose connector lifecycle state with concrete phase exit criteria instead of free-form runbooks.

## Future unlocks

1. Persist tenant-specific deprecation phase state and surface it in ByteDesk Platform connector settings.
2. Combine deprecation plans with open-work queues so operators know which Tasks block credential revocation.
3. Add provider-specific teardown adapters for webhook/subscription removal while reusing this deterministic plan as the orchestration contract.
4. Link archived evidence exports to the outcome ledger before deleting or marking credentials inert.
5. Feed deprecation plans into marketplace connector governance so agents can be delisted without losing customer history.

## Test plan

TDD red run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_deprecation_plan.py -q
```

Initial result: expected collection failure because `bytedesk_omnigent.integration_deprecation_plan` did not exist yet.

Targeted green run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_deprecation_plan.py -q
```

Result: `4 passed, 1 warning in 0.16s`.

Additional regression scope:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_deprecation_plan.py -q
```

Result: `14 passed, 1 warning in 0.19s`.

Targeted lint:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_deprecation_plan.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_deprecation_plan.py
```

Result: `All checks passed!`.

Whitespace check:

```bash
git diff --check
```

Result: passed with no output.

The pytest warning is the repository's existing `tests/known_failures.yaml` collection warning and is not introduced by this feature.
