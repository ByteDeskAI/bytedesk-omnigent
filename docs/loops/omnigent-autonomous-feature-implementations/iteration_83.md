# Autonomous feature loop iteration 83 — integration evidence assessment preview

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_83`

## Capability shipped

Iteration 83 adds a deterministic integration evidence assessment compiler and a
read-only Platform API preview for activation readiness:

- `bytedesk_omnigent.integration_evidence_assessment.assess_integration_evidence(...)`
- `POST /v1/integration-capabilities/{slug}/evidence-assessment`

Given a catalog capability slug and caller-supplied evidence items, the compiler
loads the existing verification matrix, compares supplied evidence against each
required gate, and returns a JSON-ready assessment with:

- capability identity, category, and risk tier;
- per-gate satisfied/missing evidence;
- source labels for supplied evidence;
- satisfied gate count and total gate count;
- missing evidence count and minimum required evidence count;
- a deterministic `ready_for_activation` boolean.

The surface is intentionally a preview: it does not persist evidence, inspect
secrets, call third-party providers, or mutate tenant state.

## Prior loop awareness

Before selecting this capability, I inspected open ByteDeskAI/bytedesk-omnigent
PRs with head branches matching
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- the integration capability catalog and `/v1/integration-capabilities` endpoint;
- connected-app manifests, workflow plans, handoff packages, task briefs, event
  routes, workflow harnesses, approval gates, activation gates, replay/rollback,
  rate-limit, dead-letter, retry, idempotency, backfill, OAuth/credential helpers,
  event envelopes, contract fingerprints, gap analysis, verification matrices,
  marketplace listings, staffing/demo/readiness/risk/dependency/cutover/sandbox/
  consent/verification/autonomy/incident/recommendation/evidence/routing/gap/
  pilot/acceptance/redaction/value/telemetry/tool-contract/topology/remediation
  primitives;
- provider-specific webhook ingress adapters for Slack, Stripe, GitHub, Microsoft
  Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira,
  Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow,
  Salesforce, Notion, Bitbucket, and Sentry.

This iteration deliberately does not add another provider adapter and does not
duplicate the existing verification matrix. It implements the next activation
primitive suggested by iteration 58: assess supplied evidence against the matrix
so ByteDesk Platform and autonomous harnesses can preview readiness before
persisting certification results or enabling external writes.

## Implementation details

Added:

- `bytedesk_omnigent/integration_evidence_assessment.py`
  - `IntegrationEvidenceItem`: immutable caller-provided evidence for one
    verification gate.
  - `IntegrationEvidenceItem.from_payload(...)`: safe route payload conversion
    that accepts list or scalar evidence values.
  - `assess_integration_evidence(...)`: pure deterministic compiler that returns
    `None` for unknown catalog slugs and otherwise emits a JSON-ready readiness
    assessment.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `POST /integration-capabilities/{slug}/evidence-assessment` under the
    existing catalog router.
  - Reuses the same auth boundary and 404 shape as the catalog detail and
    verification-matrix routes.

Added tests:

- `tests/bytedesk_omnigent/test_integration_evidence_assessment.py`
  - verifies partial evidence produces satisfied/missing gate details;
  - verifies complete Archon-style workflow harness evidence is activation-ready;
  - verifies unknown capability handling;
  - verifies the new HTTP preview route and 404 behavior.

## Business case

Omnigent's integration catalog and verification matrices define what should be
built and how it should be certified. The missing step for ByteDesk Platform is a
small, deterministic readiness answer: "given the evidence we have right now, can
this integration be activated safely?"

This capability improves autonomous agent integration management by giving
operators, platform UI, and workflow harnesses a shared readiness contract before
external systems are connected or write-capable tools are enabled. It helps:

- customer-facing platform screens show exactly which evidence is missing;
- autonomous builders understand the remaining acceptance gap without reading
  docs by hand;
- Archon-style deterministic workflow runs produce machine-checkable completion
  evidence;
- high-risk OAuth/service integrations stay blocked until every required gate is
  satisfied.

## Future unlocks

1. Persist evidence assessments per tenant/connector installation in an audit
   ledger.
2. Add signed evidence receipts from deterministic harness runs.
3. Combine assessment output with activation gates so external-write tools cannot
   be enabled while `ready_for_activation` is false.
4. Render readiness progress bars in ByteDesk Platform grouped by catalog
   capability and risk tier.
5. Support provider-specific evidence aliases once live connectors emit structured
   certification events.

## Test plan

TDD red run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_evidence_assessment.py -q
```

Initial result: expected collection failure because
`bytedesk_omnigent.integration_evidence_assessment` did not exist yet.

Targeted green run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_evidence_assessment.py -q
```

Result: `4 passed, 1 warning in 0.13s`.

Additional regression scope:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_evidence_assessment.py -q
```

Result: `14 passed, 1 warning in 0.18s`.

Targeted lint:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_evidence_assessment.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_evidence_assessment.py
```

Result: `All checks passed!`.

Whitespace check:

```bash
git diff --check
```

Result: passed with no output.

The pytest warning is the repository's existing `tests/known_failures.yaml`
collection warning and is not introduced by this feature.
