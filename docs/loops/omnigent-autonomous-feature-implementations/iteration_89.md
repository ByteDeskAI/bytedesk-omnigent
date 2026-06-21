# Autonomous feature loop iteration 89 — integration access-control plans

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_89`

## Capability delivered

Iteration 89 adds a deterministic access-control plan compiler for integration
capability rollout:

- `bytedesk_omnigent.integration_access_plan.compile_integration_access_plan`
- `GET /v1/integration-capabilities/{slug}/access-plan`

Given any catalog slug, Omnigent can now return a JSON-ready least-privilege
plan that ByteDesk Platform, operators, and autonomous planning agents can use
before enabling a connector. The plan includes:

- capability identity and category
- risk tier: `internal_harness`, `external_read`, or `external_write`
- least-privilege operator roles and allowed actions
- approval-required action classes
- actions blocked without approval
- read/write/offline OAuth scope review buckets

## Prior loop awareness

Before selecting this capability I inspected open loop PRs matching
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Recent
open work already covers webhook adapters, OAuth state/authorization helpers,
secret readiness, activation gates, approval plans, replay/rollback/rate-limit
plans, consent/redaction/data-boundary manifests, ownership matrices, telemetry
contracts, tool contract compilation, verification matrices, SLO profiles, and
prompt packs.

This iteration avoids duplicating those surfaces. Access-control plans fill a
remaining product gap: translating a catalog integration into concrete
least-privilege roles and blocked actions that can be rendered in ByteDesk
Platform before tenant enablement.

## Implementation description

- Added `bytedesk_omnigent.integration_access_plan` as a pure deterministic
  compiler; it does not read credentials, tenants, provider APIs, git, or GitHub.
- Added typed `AccessRole` records for JSON-stable role/action output.
- Derived risk tier from catalog category and OAuth/provider scopes:
  - workflow harness capabilities are internal harnesses;
  - capabilities with write scopes become external-write integrations;
  - remaining provider capabilities become external-read integrations.
- Added category-specific approval requirements so communication, developer,
  CRM/support, commerce, knowledge, project-management, and workflow-harness
  capabilities expose different risk controls.
- Added scope review buckets to separate read, write, and offline/refresh scopes
  for OAuth review and UI rendering.
- Exposed the compiler through the existing integration capabilities router at
  `/v1/integration-capabilities/{slug}/access-plan`, including 404 behavior for
  unknown catalog slugs.

## Business case

Customers will not enable autonomous agents inside Slack, GitHub, CRMs, support
desks, billing systems, or workflow harnesses unless the platform can clearly
show who may read, draft, approve, publish, disable, or mutate external systems.
This feature turns integration catalog strategy into an operator-facing access
model that supports enterprise trust, admin review, and safer tenant onboarding.

For ByteDesk Platform, this is a near-term UI unlock: an integration detail page
can show the access plan before OAuth install, helping admins understand the
blast radius without requiring live credentials or provider calls.

## Future unlocks

1. Platform UI cards that render access roles and approval-required actions on
   each integration capability detail page.
2. Tenant-specific role binding: map `integration_viewer`,
   `integration_operator`, `integration_approver`, `workflow_designer`, and
   `workflow_publisher` to ByteDesk workspace roles.
3. Policy engine integration where blocked actions become enforceable runtime
   policy checks before provider-side mutation.
4. OAuth app-review support by exporting read/write/offline scope buckets as
   admin review evidence.
5. Marketplace packaging: integration publishers can attach deterministic access
   plans to connector listings before customers install them.

## Test plan

TDD red step:

- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_access_plan.py -q`
- Expected failure observed before implementation:
  `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_access_plan'`

Green/verification:

- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_access_plan.py -q`
- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_access_plan.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_capabilities.py -q`
- `git diff --check`

Full suite was not run because this is a surgical pure-Python catalog/router
addition; targeted catalog/router tests cover the new behavior and adjacent
integration capability endpoints.
