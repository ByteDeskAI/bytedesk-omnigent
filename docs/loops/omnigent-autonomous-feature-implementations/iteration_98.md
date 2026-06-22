# Iteration 98 — Integration capability lifecycle plans

## Capability shipped

Added a deterministic integration capability lifecycle plan compiler and API route:

- `bytedesk_omnigent.integration_lifecycle_plan.compile_integration_lifecycle_plan(slug)`
- `GET /v1/integration-capabilities/{slug}/lifecycle-plan`

The plan converts one catalog capability into a ByteDesk/Omnigent-managed state machine for moving integrations from catalog selection through authorization, binding, validation, pilot, production activation, suspension, and retirement.

## Prior loop awareness

Before implementation I inspected open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open work already covers webhook adapters, OAuth URL/state/refresh/scope review, workflow harness compilation/validation, readiness, launch briefs, ownership, SLOs, evidence, and verification matrices through iteration 97.

To avoid duplicating those PRs, this iteration does not add a new provider adapter, OAuth compiler, launch brief, or verification gate catalog. Instead it adds the missing management state machine that ByteDesk Platform can use to consistently operate any catalog-backed integration after those artifacts exist.

## Implementation details

- Added `LifecycleStage` records with stable IDs, owners, and required evidence.
- Added risk-tier-aware stage selection:
  - `internal_harness` capabilities skip external OAuth/webhook stages and bind directly to workflow blueprints.
  - `external_read` capabilities include authorization, event binding, sandbox validation, pilot, activation, suspension, and retirement.
  - `external_write` capabilities insert a required `policy-approved` stage before pilot enablement.
- Added deterministic `allowed_transitions` so platform UI, workflow harnesses, and operators can enforce valid state changes rather than relying on ad-hoc checklists.
- Exposed the lifecycle plan under the existing authenticated integration capability router.
- Added tests for the Archon-style internal harness path, Slack external write path, unknown slugs, and the FastAPI route.

## Business case

Omnigent's mission depends on agents being safe to create, manage, coordinate, and embed into real customer systems. Integration features are only valuable if operators can activate, pause, and retire them predictably. This lifecycle plan gives ByteDesk Platform a deterministic control surface for integration rollout governance:

- Product can select catalog capabilities with a known path to activation.
- Engineering can bind OAuth/webhook/workflow implementation work to a shared state machine.
- Support and customer-success teams can suspend or retire integrations without losing historical task evidence.
- Marketplace integrations become more trustworthy because every capability has explicit evidence and ownership before production activation.

## Future unlocks

- Persist lifecycle state per tenant/integration binding and enforce transitions server-side.
- Render lifecycle plans in ByteDesk Platform as an integration setup wizard.
- Attach verification-matrix gate completion to lifecycle transitions.
- Emit lifecycle transition events into Omnigent coordination so specialist agents can claim setup, validation, and rollback tasks.
- Add policy-pack defaults per risk tier for read-only, write, and revenue-impacting actions.

## Test plan

Targeted verification run from the managed worktree:

```bash
PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_gap_analysis.py tests/bytedesk_omnigent/test_integration_lifecycle_plan.py -q
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_lifecycle_plan.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_lifecycle_plan.py
PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m mypy bytedesk_omnigent/integration_lifecycle_plan.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_lifecycle_plan.py
git diff --check
```

The first TDD run failed as expected with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_lifecycle_plan'` before production code was added. After implementation, targeted integration pytest passed (`17 passed`), targeted ruff passed, targeted mypy passed, and `git diff --check` passed.

Full-suite pytest was not run because this is a surgical catalog/API addition and the repository's full suite includes extensive e2e/LLM harness tests; targeted unit/API coverage was used for this iteration.
