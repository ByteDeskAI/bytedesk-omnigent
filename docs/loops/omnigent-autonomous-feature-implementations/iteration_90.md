# Autonomous feature loop iteration 90 — integration invocation contracts

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_90`

## Capability delivered

Iteration 90 adds a deterministic integration invocation contract compiler for
catalog-backed connected-app calls into Omnigent.

New runtime surface:

- `POST /v1/integration-capabilities/{slug}/invocation-contract`

New pure compiler:

- `bytedesk_omnigent.integration_invocation_contracts.compile_integration_invocation_contract`

Given a catalog capability slug, requester, opaque context references, and an
idempotency key, the compiler returns a JSON-ready contract with:

- execution mode (`connected_app` or `workflow_harness`)
- risk tier (`internal_harness`, `external_read`, or `external_write`)
- approval mode
- required/provided/missing context references
- category-specific routing hints
- safe activity projection channels
- link to the capability's verification matrix endpoint

The contract is intentionally deterministic and secret-free. It accepts opaque
context refs such as `office.room:abc`, `slack.channel:C456`, or
`google.drive.file:123`; it never reads credentials, provider payloads, tenant
state, GitHub, or network resources.

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs whose head branches
match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.
Open work already covers webhook adapters, OAuth/retry/idempotency/rate-limit
planning, evidence packets, verification matrices, telemetry contracts, rollout
readiness, ownership, SLO, prompt packs, and access-control plans through
iteration 89.

This iteration does not duplicate those. It builds on the catalog,
verification-matrix, and connected-app planning surfaces by answering the next
Platform question: "what exact contract must a host application satisfy before it
asks Omnigent to invoke this capability?"

## Implementation details

- Added `bytedesk_omnigent/integration_invocation_contracts.py`.
- Extended `bytedesk_omnigent/routes/integration_capabilities.py` with a POST
  endpoint under the existing integration-capability router.
- Added targeted tests in
  `tests/bytedesk_omnigent/test_integration_invocation_contracts.py`.
- Kept the change surgical: no migrations, no secrets, no external network calls,
  and no live OAuth behavior.

The compiler uses the existing integration capability catalog as source of truth
for category, scopes, and names. It applies deterministic category rules:

- workflow harness capabilities route to `workflow_harness` mode and operator
  review;
- external capabilities with mutating scopes route to mutation approval;
- read-only external capabilities can be preapproved as read-only;
- each category gets stable routing hints and context expectations.

## Business case

Connected applications need a safe, predictable way to mount Omnigent as an agent
capability runtime. Without an invocation contract, every host app would invent
its own ad-hoc request envelope, approval posture, context bundle, and activity
projection behavior.

This feature makes Omnigent easier to embed into ByteDesk Platform, Office, and
third-party surfaces such as Slack, Google Workspace, Linear/Jira, GitHub, CRMs,
and support desks. Platform teams can preflight whether they have enough context
before invoking agents, prepare the right approval UI, and project status back to
the correct app-specific channel.

## Future unlocks

1. A first-class Capability Invocation API that accepts this contract and creates
   sessions, tasks, tool runs, or workflow-harness executions.
2. ByteDesk Platform UI preflight cards that show missing context and approval
   mode before users launch an integration-backed agent.
3. App-scoped policy enforcement that consumes `risk_tier` and `approval_mode`
   directly.
4. Unified ActivityEvent projections keyed by the contract's `event_stream` and
   `status_channel` fields.
5. Deterministic Archon-style workflow harness launches from Office workflow
   templates.

## Test plan

Targeted TDD cycle:

- RED: `pytest tests/bytedesk_omnigent/test_integration_invocation_contracts.py -q`
  failed on missing `bytedesk_omnigent.integration_invocation_contracts`.
- GREEN: implemented the pure compiler and route, then reran the targeted test.
- Verification scope:
  - `pytest tests/bytedesk_omnigent/test_integration_invocation_contracts.py -q`
  - `pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_invocation_contracts.py -q`
  - `git diff --check`

Full suite was not run because this iteration only touches the deterministic
integration capability router/compiler surface and the targeted route/compiler
coverage exercises the changed paths.
