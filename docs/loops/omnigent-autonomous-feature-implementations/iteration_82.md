# Autonomous feature loop iteration 82 — integration remediation playbooks

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_82`

## Capability delivered

Iteration 82 adds deterministic integration remediation playbooks for Omnigent's
integration capability catalog and rollout verification matrix.

New API surface:

- `GET /v1/integration-capabilities/{slug}/remediation-playbook`
- Optional repeated query parameter: `failed_gate_id=<gate-id>`

The endpoint compiles a JSON-ready playbook that maps failed verification gates to
owned repair steps, evidence requirements, recommended actions, and a summary of
whether human approval is required before promotion. If no failed gate ids are
provided, the compiler returns a complete remediation playbook for every gate in
the capability's verification matrix.

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs with heads matching
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open work
already covers webhook adapters, OAuth planning, activation gates, acceptance
suites, readiness/risk/value reports, evidence packets, recommendation compilers,
telemetry contracts, tool contracts, and coordination topologies through iteration
81.

This iteration avoids duplicating those surfaces. It builds directly on the
current catalog and verification matrix by answering the next operational
question: when a gate fails, what exact deterministic repair steps should an
autonomous agent or operator run next?

## Implementation description

- Added `bytedesk_omnigent.integration_remediation_playbook`.
- Added a typed `RemediationStep` value object with JSON-ready serialization.
- Reused `compile_integration_verification_matrix` as the source of truth for
  valid gates, evidence requirements, category, and risk tier.
- Added deterministic owner routing for base gates and category-specific gates,
  including security, governance, reliability, workflow-harness, workspace, CRM,
  finance, and engineering-system ownership.
- Added deterministic recommended actions for every verification gate, always
  ending with a rerun instruction for the failed gate before promotion.
- Preserved unknown failed gate ids in the response so API callers can report bad
  operator input while still returning valid remediation steps for known gates.
- Extended `bytedesk_omnigent.routes.integration_capabilities` with the new
  authenticated read endpoint.
- Added TDD coverage for compiler behavior, unknown capability/gate handling, and
  API route behavior.

No secrets, live OAuth calls, database migrations, or network dependencies were
introduced.

## Business case

Verification matrices make rollout criteria explicit, but teams still lose time
when a rollout fails and the next corrective action is ambiguous. A deterministic
remediation playbook turns rollout failures into agent-routable work:

1. product and platform owners can see exactly who owns a failed gate;
2. autonomous implementation agents can collect the missing evidence without
   inventing a process;
3. operators can require human approval for risky external-write integrations;
4. ByteDesk Platform can surface repair instructions next to failed integration
   checks.

This directly enhances Omnigent's mission as autonomous-agent middleware: agents
can create, manage, coordinate, and repair third-party integrations with fewer
manual handoffs and safer promotion gates.

## Future unlocks

- Persist remediation playbooks as task templates for automatic repair-task
  creation when verification gates fail.
- Attach remediation owner metadata to ByteDesk Platform notifications and
  approval queues.
- Link playbook steps to evidence packets once the open evidence-packet work
  lands in develop.
- Add SLA and escalation timing per remediation owner for enterprise rollout
  dashboards.
- Let Archon-style workflow harnesses consume the playbook as deterministic
  recovery phases after failed integration test runs.

## Test plan

Targeted tests were run because the change is surgical and limited to the
ByteDesk integration-catalog extension surface:

- RED: `pytest tests/bytedesk_omnigent/test_integration_remediation_playbook.py -q`
  failed during collection because `bytedesk_omnigent.integration_remediation_playbook`
  did not exist.
- GREEN: `pytest tests/bytedesk_omnigent/test_integration_remediation_playbook.py -q`
- Regression scope: `pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_remediation_playbook.py -q`
- Lint: `ruff check bytedesk_omnigent/integration_remediation_playbook.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_remediation_playbook.py`
- Syntax: `python -m compileall bytedesk_omnigent/integration_remediation_playbook.py bytedesk_omnigent/routes/integration_capabilities.py`
- Whitespace: `git diff --check`

Full suite was not run because this iteration only adds a pure compiler, one
read-only route, and targeted API tests around the existing integration capability
router.
