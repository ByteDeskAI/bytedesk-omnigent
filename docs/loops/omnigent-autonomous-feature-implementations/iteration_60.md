# Autonomous feature loop iteration 60 — integration staffing plan compiler

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_60`

## Capability delivered

Iteration 60 adds a deterministic integration staffing plan compiler for the existing ByteDesk Omnigent integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/staffing-plan`

New Python API:

- `compile_integration_staffing_plan(slug: str)`
- `IntegrationStaffingPlan`

The compiler turns one catalog blueprint into an agent-team recommendation that ByteDesk Platform can use to pre-fill tenant rollout and agent creation flows. Each plan includes:

- primary agent role
- supporting agent roles
- coordination channels
- escalation policy
- first-30-day success outcomes
- capability business case

## Prior loop awareness

Before choosing this feature, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers webhook adapters, work intake, OAuth state/authorize/refresh/scope-review helpers, secret readiness, approval/replay/handoff/rollback/rate-limit/dead-letter/retry/idempotency/backfill plan compilers, activation gates, workflow harnesses, agent blueprint previews, credential rotation, task claims, contract fingerprints, verification matrices, gap analysis, and marketplace listings.

To avoid duplicating those PRs, iteration 60 does not add another webhook adapter, OAuth compiler, marketplace listing, verification matrix, or generic agent blueprint. Instead it adds the missing staffing layer: given a connector capability, what coordinated agent team should ByteDesk Platform create or recommend for the customer?

## Implementation details

Files changed:

- `bytedesk_omnigent/integration_capabilities.py`
  - Added `IntegrationStaffingPlan`.
  - Added category-driven staffing defaults for communication, project management, knowledge, developer, CRM/support, commerce/billing, and workflow-harness capabilities.
  - Added `compile_integration_staffing_plan(slug)` as a pure deterministic compiler with no network calls, no secrets, and no persistence.
- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Added `GET /v1/integration-capabilities/{slug}/staffing-plan`.
  - Unknown slugs fail closed with the same 404 shape used by capability detail lookups.
- `omnigent/server/API.md`
  - Documented the new endpoint and response shape.
- `tests/bytedesk_omnigent/test_integration_capabilities.py`
  - Added TDD coverage for pure compiler behavior, missing slug behavior, route response shape, and route 404 behavior.

## Business case

Omnigent's mission is autonomous agent creation, management, and coordination inside the systems where work already happens. The existing integration catalog tells ByteDesk which connectors matter; marketplace listings help sell them; agent blueprint previews describe individual agent drafts. This iteration adds a practical rollout primitive for tenant admins and platform UX: a deterministic answer to "which agent team should we staff around this integration?"

That helps ByteDesk Platform:

1. Convert integration interest into governed agent-team creation.
2. Explain the coordination model to tenant admins before OAuth connection.
3. Standardize pilot success outcomes for Slack, Notion, GitHub, Linear/Jira, Google Workspace, CRM/support, commerce, and workflow-harness integrations.
4. Keep high-risk external writes behind explicit escalation and approval language from day one.

## Future unlocks

1. Join staffing plans with tenant install state so ByteDesk Platform can show which recommended agents are already active.
2. Convert staffing roles into persisted agent templates once the agent creation API accepts catalog-derived manifests.
3. Feed first-30-day outcomes into onboarding dashboards and customer success reporting.
4. Add tenant-specific sizing inputs such as team size, event volume, and allowed write scopes.
5. Combine staffing plans with marketplace listings and verification matrices once those open loop PRs land.

## Test plan

Targeted verification was used because this change is isolated to the integration capability catalog/router/API docs.

TDD red phase:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py -q`
  - Failed as expected because `compile_integration_staffing_plan` did not exist.

Green verification:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py -q`
  - Passed: `8 passed, 1 warning in 0.18s`.

Final checks before PR:

- Targeted pytest for `tests/bytedesk_omnigent/test_integration_capabilities.py`.
- Ruff check on modified Python files.
- `git diff --check`.
