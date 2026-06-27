# Autonomous feature loop iteration 64 — integration risk register compiler

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_64`

## Capability delivered

Iteration 64 adds a deterministic integration risk register compiler for the ByteDesk Omnigent integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/risk-register`

New Python API:

- `bytedesk_omnigent.integration_risk_register.compile_integration_risk_register(slug)`
- `IntegrationRisk`

The compiler turns a catalog capability such as `slack-command-center`, `github-engineering-copilot`, or `archon-style-workflow-blueprints` into a JSON-ready operator/security risk register with:

- capability identity and category;
- inherited risk tier from the existing verification matrix;
- whether policy approval is required before activation;
- deterministic risks with severity, title, controls, and the verification gate that blocks activation until evidence exists;
- a minimum control count for UI progress and autonomous rollout scoring.

The implementation is pure, deterministic, and secret-free. It does not call third-party services, read credentials, inspect GitHub, or persist tenant data.

## Prior loop awareness

Before choosing this capability, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- provider webhook adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry;
- the integration catalog, gap analysis, verification matrices, marketplace listings, staffing plans, demo scenarios, dependency graphs, readiness assessments, OAuth/scope helpers, activation gates, approval/replay/rollback/rate-limit/dead-letter/retry/idempotency/backfill compilers, event envelopes, contract fingerprints, and workflow-harness planning.

This iteration deliberately avoids adding another provider adapter, readiness score, dependency graph, marketplace artifact, or demo surface. It adds the missing risk/control view that operators and ByteDesk Platform need before enabling autonomous agents inside external systems.

## Implementation description

Added:

- `bytedesk_omnigent/integration_risk_register.py`
  - `IntegrationRisk`: immutable risk/control model with JSON-ready serialization.
  - External-provider base risks for credential exposure, unauthorized provider writes, and event spoofing.
  - Internal workflow-harness risks for workflow drift, phase evidence gaps, and operator blindness.
  - Category-specific risks for communication, project management, knowledge, developer, CRM/support, commerce/billing, and workflow harness capabilities.
  - `compile_integration_risk_register(slug)`, which returns `None` for unknown catalog slugs and otherwise returns a deterministic register.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `GET /integration-capabilities/{slug}/risk-register` under the existing authenticated/read-only catalog router.
  - Unknown slugs return the same `not_found` error shape used by the existing detail and verification endpoints.

Added tests:

- `tests/bytedesk_omnigent/test_integration_risk_register.py`
  - verifies external-write Slack risk ordering and policy blocker behavior;
  - verifies Archon-style internal workflow harness risks avoid external-write policy blockers;
  - verifies unknown slug behavior;
  - verifies the HTTP route for GitHub engineering copilot and 404 behavior.

## Business case

Omnigent's commercial value depends on safely coordinating autonomous agents inside the tools where customers already work. As integrations move from catalog intent toward activation, customers and platform operators need more than a readiness percentage: they need an explicit risk register that explains what can go wrong and which controls must exist before activation.

This capability helps ByteDesk Platform and autonomous rollout agents by:

1. giving tenant admins a clear security/operator explanation for blocked integrations;
2. linking risks directly to existing verification gates, so evidence collection is deterministic;
3. highlighting external-write integrations that require policy approval before provider mutations;
4. giving marketplace and onboarding surfaces customer-readable controls without exposing secrets;
5. making Archon-style workflow harness rollout safer by naming workflow drift, evidence, and observability risks.

## Future unlocks

1. Persist tenant-specific risk acceptance and mitigation evidence in an audit ledger.
2. Render risk registers beside readiness assessments in ByteDesk Platform integration onboarding.
3. Require signed-off controls before enabling external-write connector actions.
4. Feed risk severity into autonomous staffing plans so security/review agents are assigned to high-risk rollout nodes.
5. Combine dependency graphs, readiness assessments, and risk registers into a full integration certification packet.

## Test plan

TDD red step:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_risk_register.py -q
```

Initial result: expected collection failure because `bytedesk_omnigent.integration_risk_register` did not exist yet.

Targeted green run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_risk_register.py -q
```

Result: `4 passed, 1 warning in 0.14s`.

Additional regression scope:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_risk_register.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_capabilities.py -q
```

Result: `14 passed, 1 warning in 0.20s`.

Targeted lint:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_risk_register.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_risk_register.py
```

Result: `All checks passed!`.

Whitespace check:

```bash
git diff --check
```

Result: passed with no output.

The pytest warning is the repository's existing `tests/known_failures.yaml` collection warning and is unrelated to this surgical integration risk register change.
