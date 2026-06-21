# Autonomous feature loop iteration 84 — integration data boundary manifests

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_84`

## Capability delivered

Iteration 84 adds deterministic integration data-boundary manifests for the
existing integration capability catalog:

- `GET /v1/integration-capabilities/{slug}/data-boundary`
- `bytedesk_omnigent.integration_data_boundary.compile_integration_data_boundary`

A data-boundary manifest answers the privacy/security handoff question every
third-party autonomous-agent integration must answer before tenant installation:

1. Which provider data classes may enter Omnigent?
2. Which provider-side mutation classes may leave Omnigent?
3. Which secret boundaries must be preserved?
4. Which audit fields must be captured for operator evidence?
5. What retention rule applies to raw provider payloads?

This directly enhances Omnigent's mission because customers can only safely let
agents operate in Slack, GitHub, Google Workspace, CRMs, support desks, commerce
systems, and workflow harnesses when the data/mutation boundary is explicit,
reviewable, and deterministic.

## Prior loop awareness

Before choosing this feature, I inspected open loop PRs whose head branches match
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open work
already covers webhook adapters, OAuth state/authorize/refresh/scope review,
activation gates, workflow harness compilation, task briefs, rollback, probes,
rate limits, dead-letter escalation, credential rotation, agent blueprint previews,
event envelopes, idempotency, retry, backfill, readiness, dependency/risk/cutover,
consent, redaction, telemetry, tool contracts, coordination topologies,
remediation playbooks, and evidence assessment previews.

This iteration intentionally avoids duplicating those. It adds a narrower missing
operator contract: a stable, catalog-derived data/mutation boundary that can be
shown before an integration is installed or an autonomous agent is granted access.
It builds on the landed catalog and verification-matrix surfaces from
`origin/develop` rather than depending on any open loop PR.

## Implementation description

- Added `bytedesk_omnigent.integration_data_boundary`.
- Added `IntegrationDataBoundary`, a JSON-ready dataclass with:
  - `capability_slug`
  - `capability_name`
  - `category`
  - `risk_tier`
  - `inbound_data_classes`
  - `outbound_mutation_classes`
  - `secret_boundaries`
  - `required_audit_fields`
  - `retention_policy`
- Derived risk tier from the existing verification matrix compiler so privacy
  and rollout evidence stay aligned.
- Added category-specific manifests for:
  - communication
  - project management
  - knowledge
  - developer
  - CRM/support
  - commerce/billing
  - workflow harness
- Added the authenticated read route under the existing integration capability
  router, preserving the route convention used by catalog and verification
  matrix endpoints.
- Added targeted unit/API tests in
  `tests/bytedesk_omnigent/test_integration_data_boundary.py`.

No secrets, credentials, migrations, network calls, or tenant data are touched.
The output is deterministic static product/security metadata.

## Business case

Autonomous agents become valuable when they can act in third-party systems, but
enterprise and SMB buyers will block adoption if they cannot understand what data
enters the agent runtime and what mutations the agent can perform. This feature
turns that risk review into a product/API surface.

The immediate business value is shorter sales and implementation cycles:
operators can show a prospective customer the exact data classes, write surfaces,
secret boundaries, audit requirements, and retention rule for each integration
before enabling it. That makes Omnigent easier to trust as managed agent
middleware and supports future ByteDesk Platform UI panels for integration
installation reviews.

## Future unlocks

1. ByteDesk Platform install wizard can render data-boundary manifests before a
   tenant enables Slack, GitHub, Notion, Google Workspace, or CRM access.
2. Policy engines can compare requested tool permissions against the outbound
   mutation classes and require approval for mismatches.
3. Evidence packets can include the manifest version used when an autonomous run
   touched an external provider.
4. Connector generators can use the manifest as a scaffold for redaction,
   retention, and audit instrumentation.
5. Marketplace listings can expose privacy/security posture for third-party
   connector templates without requiring live credentials.

## Test plan

TDD RED:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_data_boundary.py -q`
- Expected failure observed: `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_data_boundary'`.

GREEN / regression scope:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_data_boundary.py -q`
- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_gap_analysis.py tests/bytedesk_omnigent/test_integration_data_boundary.py -q`
- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_data_boundary.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_data_boundary.py`
- `git diff --check`

Full suite was not run because this is a surgical catalog/API addition with
focused unit and route coverage; the targeted integration-catalog suite covers the
changed surface.
