# Autonomous feature loop iteration 66 — integration sandbox fixtures

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_66`

## Capability delivered

Iteration 66 adds deterministic, credentialless sandbox fixture bundles for every
catalog integration capability category, exposed through:

- `GET /v1/integration-capabilities/{slug}/sandbox-fixtures`

The endpoint turns the integration capability catalog into replayable synthetic
provider events plus expected Omnigent signal contracts. Operators, Platform UI,
and autonomous loop agents can now preview how a connector should behave before
live OAuth credentials, webhook secrets, or tenant data exist.

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs with heads matching
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open work
already covers many adjacent planning artifacts and concrete adapters, including
Slack/Stripe/GitHub/Teams/Linear/Jira/Trello/Zendesk/Intercom/Google Workspace,
workflow harness compilers, approval/replay/rollback/rate-limit/idempotency/OAuth
plans, readiness assessments, verification matrices, dependency graphs, risk
registers, and cutover checklists.

This iteration deliberately avoids another checklist, readiness plan, risk
register, or provider-specific webhook adapter. Instead it adds a reusable test
fixture surface that those future adapters and plans can consume.

## Implementation description

- Added `bytedesk_omnigent.integration_sandbox_fixtures`.
- Added `IntegrationSandboxFixture`, a typed synthetic provider event contract
  with:
  - fixture id
  - operator-facing title
  - provider event name
  - expected normalized Omnigent signal type
  - deterministic assertions
- Added category-specific fixture bundles for:
  - communication
  - project management
  - knowledge
  - developer
  - CRM/support
  - commerce/billing
  - workflow harness
- Added `compile_integration_sandbox_fixtures(slug)` to resolve any catalog slug
  into a JSON-ready credentialless fixture bundle.
- Added the authenticated read endpoint under the existing integration capability
  router:
  - `/v1/integration-capabilities/{slug}/sandbox-fixtures`
- Added unit/API coverage for direct compiler behavior, category-specific project
  management fixtures, Archon-style workflow harness fixtures, unknown slug
  handling, and route exposure.

The implementation is pure and deterministic: no network calls, no credentials,
no database migrations, and no secret reads.

## Business case

Omnigent's integration moat depends on safe, repeatable connector rollout. Live
OAuth and webhook development is expensive because every provider requires tenant
setup, credential handling, and external event delivery before agents can test
basic behavior.

Sandbox fixtures lower that activation cost. Sales demos, Platform previews,
autonomous feature loops, and connector developers can validate the intended
provider-event-to-Omnigent-signal contract without touching customer systems. That
improves trust, shortens connector implementation cycles, and gives ByteDesk a
repeatable QA harness for the agent integration marketplace.

## Future unlocks

1. A Platform UI "try this integration" preview that replays the sandbox bundle
   and shows expected Tasks/signals.
2. CI contract tests for every provider adapter that must satisfy the category
   fixture assertions before shipping.
3. Synthetic demo tenants seeded from fixture bundles for sales and onboarding.
4. Archon-style deterministic workflow harnesses that consume these fixtures as
   input nodes and require completion evidence as output nodes.
5. Marketplace quality badges based on whether an integration passes its sandbox
   fixture bundle, verification matrix, and cutover checklist.

## Test plan

Targeted RED/GREEN TDD:

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_sandbox_fixtures.py -q`
  - failed with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_sandbox_fixtures'`
- GREEN: same command
  - passed: `4 passed, 1 warning`

Additional verification run before PR:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_sandbox_fixtures.py tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py -q`
- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/ruff check bytedesk_omnigent/integration_sandbox_fixtures.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_sandbox_fixtures.py`
- `PYENV_VERSION=system git diff --check`

A full repository pytest run was intentionally skipped because this is a surgical,
read-only integration metadata/API change and the targeted tests exercise the new
compiler plus neighboring integration catalog routes.
