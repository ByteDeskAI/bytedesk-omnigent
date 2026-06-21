# Autonomous feature loop iteration 61 — integration demo scenario compiler

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_61`

## Capability shipped

Iteration 61 adds a deterministic integration demo scenario compiler for the canonical ByteDesk Omnigent integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/demo-scenario`

New Python API:

- `compile_integration_demo_scenario(slug: str)`
- `IntegrationDemoScenario`

The compiler turns a catalog blueprint such as `slack-command-center` or `archon-style-workflow-blueprints` into a ByteDesk Platform-ready demonstration script with:

- capability identity and stable `demo-*` scenario slug;
- likely demo entrypoint;
- sample trigger text;
- recommended agent roles;
- deterministic demo steps;
- success metrics;
- inherited business case and future unlocks from the catalog.

## Prior loop awareness

Before selecting this feature, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Recent open loop work already covers integration staffing plans, marketplace listings, verification matrices, gap analysis, backfill plans, contract fingerprints, retry schedules, idempotency keys, event envelopes, OAuth scope review, agent blueprint previews, webhook adapters, activation gates, route compilers, approval plans, and the original integration capability catalog.

This iteration avoids duplicating those surfaces. It focuses on a missing go-to-market and onboarding artifact: an executable-style demo script that helps ByteDesk Platform, sales engineering, and tenant admins show what a capability does before credentials or production connectors exist.

## Implementation details

- Added `bytedesk_omnigent/integration_demo_scenarios.py`.
- Added a frozen `IntegrationDemoScenario` dataclass with `to_dict()` conversion that emits JSON arrays for sequence fields.
- Added deterministic mapping from catalog category to entrypoint, sample trigger, agent roles, demo steps, and success metrics.
- Added `GET /v1/integration-capabilities/{slug}/demo-scenario` to the existing integration capability router.
- The route is read-only and uses the same auth behavior as the catalog routes.
- Unknown capability slugs return the existing catalog-style `404` JSON shape.

## Business case

Demo scenarios reduce adoption friction. ByteDesk Platform can show a concrete customer-facing story for each integration capability before the customer connects OAuth credentials or deploys a production adapter.

That directly supports Omnigent's mission because it helps customers understand how autonomous agents are created, coordinated, approved, and integrated into the third-party systems where their work already lives.

For Helms/ByteDesk commercialization, this is also a marketplace enablement primitive: every integration listing can display a crisp demo path, expected agent team, and success metrics without custom copywriting or live service calls.

## Future unlocks

- Render demo scenarios in the ByteDesk Platform integration marketplace and onboarding wizard.
- Pair demo scenarios with iteration 59 marketplace listings so every connector has a launchable demo tab.
- Feed demo steps into an Archon-style workflow harness for deterministic dry-run demos.
- Extend scenarios with tenant-specific examples once customers connect Slack, Notion, Linear, Jira, Google Workspace, or CRM data.
- Add demo outcome capture so sales engineers and customer success can compare expected vs. actual onboarding behavior.

## Test plan

Targeted tests run with the canonical Omnigent virtualenv:

- RED: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_demo_scenarios.py -q`
  - Expected initial failure: `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_demo_scenarios'`.
- GREEN: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_demo_scenarios.py -q`
  - Result: `5 passed, 1 warning`.

- Compatibility: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_demo_scenarios.py tests/bytedesk_omnigent/test_integration_capabilities.py -q`
  - Result: `11 passed, 1 warning`.
- Lint: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_demo_scenarios.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_demo_scenarios.py`
  - Result: `All checks passed!`.
- Whitespace hygiene: `git diff --check`
  - Result: passed with no output.

The warning is the repository's existing `tests/known_failures.yaml` collection warning for known-failure entries that no longer match collected tests; it is unrelated to this surgical integration demo scenario change.
