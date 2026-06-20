# Autonomous feature loop iteration 63 — integration dependency graphs

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_63`

## Capability delivered

Iteration 63 adds deterministic integration dependency graphs for the ByteDesk
Omnigent integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/dependency-graph`

New compiler:

- `bytedesk_omnigent.integration_dependency_graph.compile_integration_dependency_graph(slug)`

The graph turns a catalog entry such as `linear-jira-work-intake`,
`github-engineering-copilot`, or `archon-style-workflow-blueprints` into an
ordered set of delivery milestones. Each milestone includes:

- stable node id
- title
- dependency ids
- concrete deliverables

This gives autonomous loop planners and ByteDesk Platform UI a deterministic
implementation path before an integration is considered ready for the existing
verification matrix.

## Prior loop awareness

Before choosing this iteration's feature, I inspected open loop PRs matching
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.
Relevant open work included:

- #158 iteration 62: integration readiness assessments
- #157 iteration 61: integration demo scenarios
- #156 iteration 60: integration staffing plans
- #155 iteration 59: integration marketplace listings
- #152 iteration 56: integration backfill plan compiler
- #151 iteration 55: integration contract fingerprints
- #150 iteration 54: integration retry schedule compiler
- #149 iteration 53: integration idempotency key compiler
- #148 iteration 52: integration event envelopes
- #147 iteration 51: integration OAuth scope review
- #146 iteration 50: integration agent blueprint previews
- #118 iteration 22: integration workflow harness compiler
- #100 iteration 4: connected app manifest compiler
- #99 iteration 3: integration workflow plan compiler
- #98 iteration 2: external work item intake

To avoid duplicating those surfaces, this iteration does not add another readiness
score, demo, staffing plan, marketplace listing, retry/idempotency plan, OAuth
review, or adapter. It adds the missing dependency graph between catalog intent
and rollout verification: the ordered prerequisites an implementation loop should
complete before the existing verification matrix is meaningful.

## Implementation description

- Added `bytedesk_omnigent.integration_dependency_graph` with:
  - `IntegrationDependencyNode`, a JSON-ready dataclass for delivery milestones.
  - category-aware dependency graphs for communication, project management,
    knowledge, developer, CRM/support, commerce/billing, and workflow-harness
    capabilities.
  - special Archon-style workflow-harness sequencing:
    `catalog-contract -> workflow-schema -> phase-compiler -> verification-harness -> operator-observability`.
  - provider integration sequencing:
    `catalog-contract -> auth-sandbox -> webhook-ingress -> category mapping -> policy-and-idempotency -> operator-observability`.
- Extended `bytedesk_omnigent.routes.integration_capabilities` with the read-only
  dependency-graph endpoint, preserving the existing auth behavior via
  `require_user`.
- Added focused unit/API tests covering:
  - Archon-style workflow dependency graph shape.
  - Linear/Jira provider graph shape and auth-model deliverables.
  - unknown slug handling.
  - FastAPI route success and 404 behavior.

The implementation is deterministic and secret-free. It performs no live OAuth,
network, GitHub, or tenant-data calls.

## Business case

Omnigent's product value depends on making autonomous integrations repeatable,
safe, and easy to coordinate. The catalog says what to build; the verification
matrix says how to prove it. The new dependency graph fills the operational gap:
it tells a planning agent, operator, or platform UI what must exist first.

This helps ByteDesk Platform integration because it can show customers and
operators a clear rollout path for high-value connectors like GitHub, Linear/Jira,
Slack, Notion, and Google Workspace without exposing secrets or requiring the
connector to already be live.

It also improves autonomous agent management by giving implementation loops a
stable, machine-readable task decomposition. Future agents can select a catalog
entry, read its dependency graph, and turn each node into an auditable Omnigent
Task with predictable prerequisites.

## Future unlocks

1. Platform UI can render dependency graphs as integration rollout checklists.
2. Autonomous planners can convert graph nodes into Omnigent Tasks and assign
   specialist agents by category.
3. Readiness assessments can cite completed dependency nodes as evidence instead
   of free-text status.
4. Marketplace listings can expose implementation maturity by dependency-node
   completion.
5. Workflow-harness graphs can evolve into Archon-style deterministic execution
   blueprints with dry-run validation and phase evidence.
6. ByteDesk tenant admins can compare dependency graph state against installed
   connectors to identify safe next actions.

## Test plan

TDD red step:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_dependency_graph.py -q`
- Expected failure observed before implementation:
  `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_dependency_graph'`

Targeted verification:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_dependency_graph.py -q`
  - Result: 4 passed, 1 warning.
- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_gap_analysis.py tests/bytedesk_omnigent/test_integration_dependency_graph.py -q`
  - Result: 17 passed, 1 warning.

I did not run the full repository suite because this is a surgical, read-only
catalog/API addition and the related catalog/compiler tests cover the changed
surface. I ran `git diff --check` before committing.
