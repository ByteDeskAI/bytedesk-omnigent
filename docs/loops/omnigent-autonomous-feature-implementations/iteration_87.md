# Autonomous feature loop iteration 87 — integration SLO profiles

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_87`

## Capability delivered

Iteration 87 adds deterministic integration SLO profiles for every cataloged
integration capability. Operators and ByteDesk Platform surfaces can now query:

- `GET /v1/integration-capabilities/{slug}/slo-profile`

The profile compiles the existing integration catalog entry into a secret-free,
JSON-ready reliability promise with:

- risk tier (`internal_harness`, `external_read`, or `external_write`)
- availability target
- sync freshness target
- action latency target
- measurement events
- category-specific operational controls
- operator promises
- error-budget freeze/page thresholds

## Prior loop awareness

Before selecting this capability, iteration 87 inspected open loop PRs with heads
matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.
The open work already covers webhook adapters, workflow harnesses, OAuth planning,
readiness assessments, verification matrices, telemetry contracts, value
scorecards, data boundaries, remediation plans, ownership matrices, and
integration deprecation plans through iteration 86.

This iteration intentionally avoids duplicating those surfaces. It complements
prior verification and telemetry work by answering a different production launch
question: what reliability promise should ByteDesk and an operator expect once an
integration is enabled?

## Implementation description

- Added `bytedesk_omnigent.integration_slo_profiles` as a pure deterministic
  compiler over the existing `integration_capabilities` catalog.
- Added `compile_integration_slo_profile(slug)` returning `None` for unknown
  slugs and a JSON-ready profile for known catalog capabilities.
- Reused catalog category and scope metadata to derive risk tiers without live
  credentials, network calls, tenant data, or secret reads.
- Added route support to `bytedesk_omnigent.routes.integration_capabilities` at
  `/integration-capabilities/{slug}/slo-profile` under the existing `/v1` router.
- Added targeted unit/API tests covering internal workflow harnesses, external
  write integrations, developer category controls, and 404 behavior.

## Business case

Integration buyers need more than connector availability: they need confidence
that autonomous agents will operate predictably inside business systems. SLO
profiles make that promise explicit before launch. ByteDesk Platform can show
operators the expected freshness, latency, evidence, and freeze behavior for
Slack, GitHub, Google Workspace, CRMs, commerce systems, and Archon-style
workflow harnesses.

This improves Omnigent's mission as autonomous-agent middleware by giving the
platform a deterministic operational contract for each integration, reducing
enterprise adoption risk, and turning reliability posture into a product-visible
asset instead of tribal knowledge.

## Future unlocks

1. Platform UI launch cards that show SLO targets alongside verification gates.
2. Tenant-level overrides for paid tiers while keeping catalog defaults stable.
3. Automated freeze enforcement when integration telemetry crosses the compiled
   error-budget thresholds.
4. CI checks that require each new integration adapter to emit the measurement
   events named by its SLO profile.
5. Marketplace badges for integrations that have met their SLO profile during a
   pilot window.

## Test plan

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_slo_profiles.py -q`
  - Expected failure observed before implementation: missing
    `bytedesk_omnigent.integration_slo_profiles` module.
- GREEN targeted test: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_slo_profiles.py -q`
  - Result: 4 passed, 1 existing known-failures warning.

Additional verification performed before PR creation is recorded in the PR body
and terminal transcript.
