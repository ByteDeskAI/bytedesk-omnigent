# Autonomous feature loop iteration 97 — integration launch briefs

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_97`

## Capability delivered

Iteration 97 adds deterministic integration launch briefs for catalog capabilities:

- `GET /v1/integration-capabilities/{slug}/launch-brief`
- `bytedesk_omnigent.integration_launch_brief.compile_integration_launch_brief(slug)`

The brief turns the existing integration catalog and verification matrix into an operator-facing launch sequence: recommended launch mode, credential posture, scope review requirement, rollout phases, required gates per phase, exit criteria, and a default success metric.

## Prior loop awareness

Before choosing this feature, I inspected open loop PRs matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Recent open work already covers verification matrices, workflow blueprint validation, Teams blueprints, manifests, onboarding questionnaires, invocation contracts, access plans, prompt packs, SLOs, deprecation plans, ownership matrices, data-boundary manifests, evidence previews, remediation playbooks, coordination topologies, tool contract compilation, telemetry contracts, value scorecards, redaction profiles, acceptance suites, pilot plans, gap analysis, tenant routing, evidence packets, recommendations, incident drills, autonomy policies, verification assessments, consent manifests, fixtures, cutover, risk registers, dependency graphs, readiness assessments, demo scenarios, staffing, marketplace listings, and many webhook/OAuth adapter surfaces.

This iteration does not duplicate those PRs. It builds on the landed integration catalog and verification matrix by producing a compact launch brief that a Platform UI, autonomous planner, or operator can use to decide how to safely move a catalog item from blueprint to tenant rollout.

## Implementation details

- Added `bytedesk_omnigent.integration_launch_brief`.
- The compiler is pure and deterministic: no git, GitHub, tenant data, credentials, or provider network calls.
- The compiler consumes `compile_integration_verification_matrix(slug)` so the launch brief stays aligned with catalog metadata, risk tier, auth model, scopes, and category-specific gates.
- Internal workflow-harness capabilities receive a no-external-credential launch path:
  - contract
  - harness dry run
  - operator review
  - production enablement
- External integrations receive a provider rollout path:
  - contract
  - OAuth sandbox
  - read-only pilot
  - approved write pilot for `external_write` capabilities only
  - production enablement
- Added the `/launch-brief` route beside existing catalog detail and verification-matrix routes, preserving the same auth behavior and 404 shape.
- Added targeted unit/API tests for internal harness, external write, unknown slug, and FastAPI route behavior.

## Business case

Omnigent’s integration catalog explains what to build, and the verification matrix explains what evidence is required. Operators and product surfaces still need a practical launch sequence that answers: “How do we safely turn this capability on?”

Launch briefs close that gap. They make third-party integrations and workflow harnesses easier to package for ByteDesk Platform by turning product strategy into deterministic rollout steps with explicit safety gates. This reduces integration enablement risk, shortens implementation planning time, and gives tenant-facing UIs a stable read model for connector launch readiness.

## Future unlocks

1. Platform UI can render launch phases and gate completion status per tenant.
2. Autonomous planning agents can generate tasks directly from launch brief phases.
3. Evidence collection can mark each phase complete using outcome records and verification packets.
4. The gap-analysis API can rank not only what to build next, but what is closest to launch.
5. Integration marketplace listings can expose launch posture without leaking secrets or provider credentials.

## Test plan

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_launch_brief.py -q` initially failed because `bytedesk_omnigent.integration_launch_brief` did not exist.
- GREEN/regression: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_launch_brief.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_capabilities.py -q`
  - Result: `14 passed, 1 warning in 0.20s`
- `ruff check bytedesk_omnigent/integration_launch_brief.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_launch_brief.py`
- `git diff --check`
