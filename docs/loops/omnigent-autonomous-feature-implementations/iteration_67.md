# Autonomous feature loop iteration 67 — integration consent manifests

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_67`

## Capability delivered

Iteration 67 adds deterministic, credentialless integration consent manifests for
the canonical integration capability catalog, exposed through:

- `GET /v1/integration-capabilities/{slug}/consent-manifest`

The endpoint converts a catalog capability into activation-ready consent copy,
scope rationales, operator disclosures, and category-specific risk prompts. This
helps ByteDesk Platform explain what an Omnigent integration will access before a
customer authorizes OAuth, installs an app, or enables an internal workflow
harness.

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs with heads matching
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open work
already covers provider webhook ingress adapters, approval/replay/rollback/rate
limit/idempotency/OAuth planning, verification matrices, readiness assessments,
dependency graphs, risk registers, cutover checklists, marketplace listings,
staffing plans, demo scenarios, and sandbox fixtures.

This iteration avoids duplicating those surfaces. It focuses specifically on the
pre-authorization consent and disclosure artifact that Platform UI and autonomous
operators need before any live third-party credentials are requested.

## Implementation description

- Added `bytedesk_omnigent.integration_consent_manifest`.
- Added `ScopeRationale`, a typed JSON-ready explanation for one requested
  provider scope.
- Added `compile_integration_consent_manifest(slug)` to resolve any catalog
  capability into:
  - capability metadata;
  - human-readable consent summary;
  - operator disclosure copy;
  - per-scope rationales with low/moderate/high risk labels;
  - category-specific risk prompts for communication, project-management,
    knowledge, developer, CRM/support, commerce/billing, and workflow-harness
    capabilities.
- Added the authenticated read endpoint under the existing integration capability
  router:
  - `/v1/integration-capabilities/{slug}/consent-manifest`
- Updated `omnigent/server/API.md` with the new endpoint contract.
- Added unit/API coverage for Google Workspace scope rationales, credentialless
  Archon-style workflow harness activation, unknown slug handling, and route
  exposure.

The implementation is pure and deterministic: no network calls, no credentials,
no database migrations, and no secret reads.

## Business case

Customer trust is a gating factor for autonomous agent integration. Even if the
technical connector works, buyers need clear explanations of which scopes an
agent workforce will request, what those scopes enable, and which actions require
operator approval.

Consent manifests turn the integration catalog into a Platform-ready onboarding
surface. Sales demos, admin setup screens, marketplace listings, and autonomous
connector builders can show least-privilege rationale before OAuth starts. That
reduces security-review friction, improves conversion for third-party app
connectors, and makes Omnigent safer to embed into ByteDesk Platform as managed
agent middleware.

## Future unlocks

1. Platform admin UI can render consent manifests before launching OAuth.
2. Marketplace listings can display scope rationales and risk prompts directly
   from the same catalog-backed contract.
3. OAuth activation flows can require operators to acknowledge category-specific
   risk prompts before credentials are stored.
4. Verification matrices can include consent-manifest review as a required gate
   for production rollout.
5. Future provider adapters can compare requested live scopes against the
   manifest to detect over-broad authorization attempts.

## Test plan

Targeted RED/GREEN TDD:

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_consent_manifest.py -q`
  - failed as expected with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_consent_manifest'`
- GREEN: same command
  - passed: `4 passed, 1 warning`

Additional verification run before PR:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_consent_manifest.py tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py -q`
  - passed: `14 passed, 1 warning`
- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/ruff check bytedesk_omnigent/integration_consent_manifest.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_consent_manifest.py`
  - passed: `All checks passed!`
- `PYENV_VERSION=system git diff --check`
  - passed

A full repository pytest run was intentionally skipped because this is a surgical,
read-only integration metadata/API change and the targeted tests exercise the new
compiler plus neighboring integration catalog routes.
