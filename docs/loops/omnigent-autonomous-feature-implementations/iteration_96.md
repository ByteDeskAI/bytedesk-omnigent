# Autonomous feature loop iteration 96 — integration workflow blueprint validator

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_96`

## Capability shipped

Iteration 96 adds a deterministic integration workflow blueprint validator for Omnigent's catalog-driven integration work.

New API surface:

- `POST /v1/integration-capabilities/workflow-blueprints/validate`

New internal validator:

- `bytedesk_omnigent.integration_workflow_blueprint_validator.validate_integration_workflow_blueprint`

The validator accepts an Archon-style phase graph for any catalog capability and returns a JSON-ready validation report with:

- catalog traceability through `capability_slug`;
- deterministic phase node IDs;
- required phase role, input, output, and completion-evidence checks;
- duplicate phase ID detection;
- missing/self dependency detection;
- cycle detection for phase dependency graphs;
- a stable `valid` boolean and issue list for UI, agents, or automation.

## Prior loop awareness

Before selecting this capability, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- provider-specific webhook adapters for Slack, Stripe, GitHub, Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry;
- planning and operational compilers for workflow plans, handoff packages, task briefs, event routes, OAuth helpers, activation gates, replay/rollback/rate-limit/dead-letter/retry/idempotency/backfill plans, contract fingerprints, readiness, dependency graphs, staffing, marketplace listings, cutover, sandbox fixtures, consent, verification, autonomy, incident drills, recommendations, evidence packets, routing, gap analysis, pilot plans, acceptance suites, redaction, value scorecards, telemetry, tool contracts, topologies, remediation, evidence assessment, data boundaries, ownership, deprecation, SLOs, prompt packs, access control, invocation contracts, questionnaires, bundles, configuration manifests, and Teams blueprints.

This iteration deliberately does not add another provider adapter or another rollout/readiness compiler. It adds the missing preflight harness primitive: a deterministic validator that rejects malformed workflow-blueprint phase graphs before agents attempt to execute them.

## Implementation details

Added:

- `bytedesk_omnigent/integration_workflow_blueprint_validator.py`
  - Defines `BlueprintValidationIssue` for stable JSON-ready validation issues.
  - Adds `validate_integration_workflow_blueprint(blueprint)` as a pure, secret-free validator.
  - Validates catalog capability existence, at least one phase, stable node IDs, duplicate IDs, role/input/output/completion evidence presence, missing/self dependencies, and dependency cycles.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `POST /integration-capabilities/workflow-blueprints/validate` under the existing authenticated/local-open integration capabilities router.
  - Returns validation reports with HTTP 200 for both valid and invalid blueprints so UI and autonomous loops can show structured corrections instead of treating design-time validation as transport failure.

Added tests:

- `tests/bytedesk_omnigent/test_integration_workflow_blueprint_validator.py`
  - Validates a deterministic Archon-style workflow blueprint.
  - Verifies duplicate IDs, missing dependencies, dependency cycles, missing completion evidence, and missing inputs are reported.
  - Verifies the API route exposes the validator and returns structured invalid reports for unknown catalog capabilities.

## Business case

Omnigent's mission is to create, manage, coordinate, and integrate autonomous agents. Catalog entries explain what integrations matter, and verification matrices explain how to prove rollout readiness. The new validator closes the step between planning and execution: it ensures a proposed multi-agent workflow has deterministic phases and auditable completion evidence before work is handed to agents.

That is valuable for ByteDesk Platform because customers can safely author repeatable agent workflows for Slack, GitHub, Google Workspace, CRMs, support desks, commerce systems, or internal harnesses without relying on ad-hoc prompt structure. Invalid graphs become actionable correction reports instead of failed autonomous runs.

## Future unlocks

1. Persist validated workflow blueprints as marketplace-ready templates.
2. Add a platform UI form that validates phase graphs while customers author workflows.
3. Connect validator output to future workflow compilers so only valid graphs become Omnigent Tasks.
4. Add optional severity levels for non-blocking best-practice warnings such as missing rollback phase or missing human approval phase.
5. Combine validation reports with verification matrices to certify full connector workflows before tenant activation.

## Test plan

RED test first:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_workflow_blueprint_validator.py -q
```

Expected failure before implementation:

- `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_workflow_blueprint_validator'`

Targeted tests after implementation:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_workflow_blueprint_validator.py -q
```

Result: `3 passed, 1 warning`.

Additional verification run for the touched integration capability surface:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_workflow_blueprint_validator.py -q
```

Lint/diff verification:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_workflow_blueprint_validator.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_workflow_blueprint_validator.py
git diff --check
```
