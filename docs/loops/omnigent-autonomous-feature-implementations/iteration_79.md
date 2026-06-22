# Autonomous feature loop iteration 79 — integration telemetry contracts

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_79`

## Capability delivered

Iteration 79 adds deterministic integration telemetry contracts for every catalog
capability, exposed through:

- `GET /v1/integration-capabilities/{slug}/telemetry-contract`

The compiler turns a catalog entry into a JSON-ready observability contract that
names the trace fields, normalized events, metric prefix, and operator health
indicators required to run that integration safely. It covers both Archon-style
internal workflow harness capabilities and external OAuth/service integrations.

## Prior loop awareness

Before selecting the feature, I inspected open loop PRs with heads matching
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Existing
open work already covers webhook adapters, OAuth plans, event route compilers,
verification matrices, acceptance suites, redaction profiles, scorecards, and
other readiness/cutover artifacts.

To avoid duplicating those, iteration 79 focuses on the missing observability
contract layer: what adapters and deterministic workflow harnesses must emit at
runtime so operators and autonomous planning loops can prove an integration is
healthy without exposing secrets or provider payloads.

## Implementation description

- Added `bytedesk_omnigent.integration_telemetry_contract` as a pure compiler.
- Added typed telemetry dataclasses for normalized event contracts and health
  indicators.
- Added risk-tier-aware contracts:
  - `internal_harness` capabilities emit workflow phase start/complete/fail
    events with workflow, phase, task, agent, and evidence trace fields.
  - `external_read` capabilities emit ingress/read telemetry suitable for
    least-privilege read-only connectors.
  - `external_write` capabilities emit ingress, policy-check, dispatch, and
    failure telemetry that ties provider mutations back to task, agent, approval,
    and action identifiers.
- Added `/telemetry-contract` under the existing integration capability router.
- Kept the feature deterministic and secret-free: no credentials, no network
  calls, no database migration, and no provider payload inspection.
- Added targeted unit/API tests for workflow harness contracts, external write
  policy telemetry, unknown capability handling, and the FastAPI route.

## Business case

Omnigent's core promise is not just that agents can call tools; it is that teams
can safely manage autonomous agent work across Slack, GitHub, Google Workspace,
CRMs, commerce systems, and deterministic workflow harnesses. Customers will need
operator-visible proof that integrations are healthy, policy-gated, auditable,
and safe to scale.

Telemetry contracts give ByteDesk Platform and Omnigent operators a deterministic
spec for dashboards, alerts, support triage, and customer-facing trust evidence.
That shortens enterprise pilots because every connector can ship with the same
minimum observability surface instead of ad-hoc logs.

## Future unlocks

1. Generate OpenTelemetry semantic conventions directly from each telemetry
   contract.
2. Add a platform UI that shows contract coverage per installed integration.
3. Feed contract health indicators into rollout gates before enabling write
   scopes for a tenant.
4. Let autonomous planning agents compare verification matrices, telemetry
   contracts, and live evidence to produce production-readiness reports.
5. Add alert routing templates for Slack, PagerDuty, Linear, or ByteDesk tasks
   based on the generated metric prefixes.

## Test plan

- RED: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_telemetry_contract.py -q`
  - Expected failure before implementation: `ModuleNotFoundError` for
    `bytedesk_omnigent.integration_telemetry_contract`.
- GREEN: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_telemetry_contract.py -q`
  - Result: 4 passed, 1 existing known-failures warning.
- Targeted regression: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_gap_analysis.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_telemetry_contract.py -q`
- Static/diff checks: `git diff --check`
