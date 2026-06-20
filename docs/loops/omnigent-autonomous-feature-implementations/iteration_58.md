# Autonomous feature loop iteration 58 — integration verification matrix compiler

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_58`

## Capability shipped

Iteration 58 adds a deterministic integration verification matrix compiler for the canonical integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/verification-matrix`

New internal compiler:

- `bytedesk_omnigent.integration_verification_matrix.compile_integration_verification_matrix(slug)`

Given a catalog slug such as `slack-command-center`, `google-workspace-operator`, or `archon-style-workflow-blueprints`, the compiler returns a JSON-ready rollout checklist with:

- capability identity and category;
- risk tier (`internal_harness`, `external_read`, or `external_write`);
- auth model and required scopes copied from the catalog;
- shared evidence gates for catalog contract, auth boundary, ingress normalization, replay/idempotency, policy approval, observability, and rollback readiness;
- one category-specific gate for communication, project-management, knowledge, developer, CRM/support, commerce/billing, or workflow-harness capabilities;
- a `minimum_required_evidence_count` for deterministic completion scoring.

## Prior loop awareness

Before selecting this capability, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- the integration capability catalog and `/v1/integration-capabilities` endpoint;
- connected app manifests, workflow plans, handoff packages, task briefs, event routes, workflow harnesses, approval gates, activation gates, replay/rollback/rate-limit/dead-letter/retry/idempotency/backfill compilers, OAuth/credential helpers, event envelopes, contract fingerprints, and gap analysis;
- provider-specific webhook ingress adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry.

This iteration deliberately does not add another provider adapter and does not duplicate the recent gap analyzer. It adds the next missing operational primitive: a deterministic readiness matrix that tells autonomous loops and platform operators what evidence must exist before any catalog capability is considered safely integrated.

## Implementation details

Added:

- `bytedesk_omnigent/integration_verification_matrix.py`
  - `VerificationGate`: a small immutable evidence-gate model with JSON-ready serialization.
  - Shared rollout gates that apply to every catalog integration.
  - Category-specific gates for all current `CapabilityCategory` values.
  - Risk-tier derivation from the catalog category and required scopes.
  - `compile_integration_verification_matrix(slug)`, which returns `None` for unknown catalog slugs and otherwise returns a deterministic dict.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `GET /integration-capabilities/{slug}/verification-matrix` under the existing authenticated/read-only catalog router.
  - Unknown slugs return the same `not_found` error shape used by the existing detail endpoint.

Added tests:

- `tests/bytedesk_omnigent/test_integration_verification_matrix.py`
  - verifies Archon-style workflow harness gates and evidence counts;
  - verifies provider-specific Slack scope and communication-loop gates;
  - verifies unknown slug behavior;
  - verifies the new HTTP route for Google Workspace and 404 behavior.

## Business case

Omnigent's integration catalog now has many possible connectors and many open autonomous loop PRs. The next business bottleneck is not only choosing what to build; it is proving that integrations are safe, auditable, deterministic, and ready for customer activation.

The verification matrix gives ByteDesk Platform and autonomous loop workers a consistent acceptance contract:

- product can show customers why an integration is not yet activation-ready;
- operators can require concrete evidence before enabling external writes;
- autonomous builders can generate work plans from the same readiness gates;
- marketplace connector reviews can compare providers against a uniform checklist;
- workflow-harness features inspired by Archon can demand stable phase graphs, typed inputs/outputs, and terminal completion evidence.

## Future unlocks

1. Add an evidence ingestion endpoint that marks individual gates as satisfied per tenant or connector installation.
2. Combine the iteration 57 gap analyzer with this matrix so the next recommended integration also includes its readiness checklist.
3. Render capability readiness in ByteDesk Platform as a progress bar grouped by risk tier and evidence gate.
4. Bind category-specific gates to deterministic workflow harness phases, enabling repeatable connector certification runs.
5. Store verification results in an audit ledger before allowing high-risk connector activation.

## Test plan

TDD red run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_verification_matrix.py -q
```

Initial result: expected collection failure because `bytedesk_omnigent.integration_verification_matrix` did not exist yet.

Targeted green run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_verification_matrix.py -q
```

Result: `4 passed, 1 warning in 0.13s`.

Additional regression scope:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py -q
```

Result: `10 passed, 1 warning in 0.16s`.

Targeted lint:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_verification_matrix.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py
```

Result: `All checks passed!`.

Whitespace check:

```bash
git diff --check
```

Result: passed with no output.

The pytest warning is the repository's existing `tests/known_failures.yaml` collection warning and is not introduced by this feature.
