# Omnigent autonomous feature loop iteration 76

## Capability shipped

Added an integration acceptance suite compiler for the integration capability catalog.

The new pure compiler turns a catalog capability such as `slack-command-center`, `github-engineering-copilot`, or `archon-style-workflow-blueprints` into deterministic, secret-free acceptance scenarios that can be consumed by autonomous planning loops, ByteDesk Platform UI, or a future workflow harness before enabling an integration for a tenant.

New API surface:

- `GET /v1/integration-capabilities/{slug}/acceptance-suite`

New implementation files:

- `bytedesk_omnigent/integration_acceptance_suite.py`
- `tests/bytedesk_omnigent/test_integration_acceptance_suite.py`

## Prior loop awareness

Before choosing this iteration, I inspected open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. The open loop work already covers provider webhook adapters, OAuth planning, replay/idempotency, rate limits, dead-letter escalation, credential rotation, blueprint previews, readiness/risk/cutover/staffing/demo/marketplace/dependency/recommendation/evidence/pilot-plan surfaces, and iteration 75's verification matrix.

This iteration intentionally does not add another provider adapter or duplicate the verification matrix. It builds on the catalog and verification matrix by compiling concrete acceptance scenarios: the next deterministic layer between "what must be true" and "what a harness should exercise".

The canonical checkout had unrelated WIP, so the managed workflow operator initially refused to create the worktree. I reran the same managed operator with `--allow-dirty` as directed by the loop skill, leaving the canonical WIP untouched.

## Implementation details

`compile_integration_acceptance_suite(slug)` returns `None` for unknown slugs and otherwise emits a JSON-ready manifest with:

- `object`: `integration_acceptance_suite`
- catalog identity and provider category
- risk tier from the existing verification matrix compiler
- auth model and required scopes
- `minimum_passing_scenarios`
- ordered deterministic scenarios with stable ids, modes, titles, and expected evidence

All suites include base scenarios for:

1. Static catalog contract loading without network or secrets.
2. Authorization boundary declaration before execution.

External provider suites then add scenarios for:

1. Provider event normalization into Omnigent signals.
2. Idempotent replay of duplicate provider delivery.
3. Policy-gated provider writes.
4. Category-specific acceptance evidence for communication, project management, knowledge, developer, CRM/support, or commerce/billing integrations.

Workflow harness suites add Archon-style deterministic workflow scenarios for:

1. Successful blueprint compilation into stable task graphs.
2. Fail-closed phase behavior that prevents downstream mutation after a failed prerequisite.

The route uses the existing authenticated integration capability router and returns the same `not_found` error shape as the catalog detail and verification matrix routes.

## Business case

Omnigent's integration roadmap needs deterministic proof that a connector is safe to activate before real tenants authorize Slack, GitHub, Google Workspace, CRM, billing, or workflow-harness access. The acceptance suite gives platform operators and autonomous agents a productized checklist of executable scenarios without requiring live credentials or secrets.

This reduces integration delivery risk, shortens customer onboarding, and creates a repeatable evidence artifact that can be shown in ByteDesk Platform during integration setup, pilot readiness review, and enterprise security discussions.

## Future unlocks

- Generate executable pytest or workflow-harness cases directly from acceptance suite manifests.
- Attach pass/fail acceptance evidence to tenant integration activation records.
- Let ByteDesk Platform display acceptance coverage next to catalog entries and pilot plans.
- Feed acceptance failures into autonomous repair tasks for provider adapters.
- Combine acceptance suites with iteration 75 verification matrices to produce go/no-go rollout dashboards.

## Test plan

Targeted TDD and verification were run from the managed iteration 76 worktree using the canonical virtualenv and `PYTHONPATH=$PWD`.

- RED: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_acceptance_suite.py -q`
  - Failed as expected with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_acceptance_suite'`.
- GREEN targeted suite: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_acceptance_suite.py -q`
  - Passed: 4 tests.
- Broader related tests: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_acceptance_suite.py tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_gap_analysis.py -q`
  - Passed: 17 tests.
- Lint: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_acceptance_suite.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_acceptance_suite.py`
  - Passed: all checks passed.
- Static sanity: `git diff --check`
  - Passed: no whitespace errors.
