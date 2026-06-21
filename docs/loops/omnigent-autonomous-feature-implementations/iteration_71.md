# Autonomous feature loop iteration 71 — integration capability recommendations

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_71`

## Capability delivered

Iteration 71 adds a deterministic integration capability recommendation compiler and API route:

- `GET /v1/integration-capabilities/recommendations?goal=...`

Operators, planning agents, and ByteDesk Platform surfaces can now submit a natural-language business goal and receive ranked catalog matches with match scores, matched signals, rationale, and the full underlying capability blueprint.

This directly enhances Omnigent's mission by helping autonomous loops and users choose which third-party or workflow-harness integration should be activated or built for a specific agent workforce goal without using live credentials, LLM calls, network calls, or tenant data.

## Prior loop awareness

Before choosing this capability, I inspected open loop PRs matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Recent open work already covers incident drills, autonomy policies, verification assessments, consent manifests, sandbox fixtures, cutover checklists, risk registers, dependency graphs, readiness assessments, demo scenarios, staffing plans, marketplace listings, webhook adapters, OAuth helpers, workflow harness compilers, and verification matrices.

This iteration intentionally avoids duplicating those artifacts. It adds a goal-scored recommendation layer on top of the existing `/v1/integration-capabilities` catalog so future loops and ByteDesk Platform UI can select among those catalog capabilities for a concrete customer objective.

## Implementation description

- Added `bytedesk_omnigent.integration_recommendations`:
  - `IntegrationCapabilityRecommendation`
  - `IntegrationCapabilityRecommendationReport`
  - `recommend_integration_capabilities(goal, category=None, limit=3)`
- The scorer is deterministic and local:
  - tokenizes the submitted goal
  - expands a small set of aliases such as `ci`, `pr`, `docs`, and `tickets`
  - compares goal tokens with catalog metadata and category-specific hints
  - uses catalog priority score as the stable tie-breaker
- Added `GET /v1/integration-capabilities/recommendations` to the existing integration capability router.
- The route is mounted before `/{slug}` so the static recommendations path is not consumed as a capability slug.
- Blank goals return HTTP 422.
- No secrets, OAuth credentials, live provider calls, database migration, or network access are introduced.

## Business case

Customers rarely start with a connector slug. They start with a business goal such as:

- "Route failed CI and review comments into autonomous engineering repair tasks"
- "Import Notion docs into autonomous agent memory"
- "Create customer support triage agents that draft responses from tickets"

This feature turns those goals into deterministic integration recommendations that ByteDesk Platform can show directly in onboarding, agent creation, and integration setup flows. It reduces discovery friction, keeps Omnigent's integration roadmap productized, and gives autonomous implementation loops a machine-readable way to pick high-value next work without duplicating prior PRs.

## Future unlocks

1. Add a Platform UI panel that asks for a business goal and renders the recommended integration cards.
2. Feed recommendation reports into agent blueprint generation so users can create integration-ready agents from natural language.
3. Combine recommendations with gap analysis and open PR signals to automatically skip in-flight work.
4. Add optional organization-specific weighting once tenant policy and installed connectors are available.
5. Use recommendation reports as the first step in an Archon-style deterministic workflow that compiles goal → connector → verification matrix → rollout plan.

## Test plan

Targeted tests and lint were run from the managed iteration 71 worktree.

- RED: `pytest tests/bytedesk_omnigent/test_integration_recommendations.py -q`
  - Failed as expected with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_recommendations'` before implementation.
- GREEN/regression: `pytest tests/bytedesk_omnigent/test_integration_recommendations.py tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_gap_analysis.py -q`
  - `17 passed, 1 warning in 0.20s`
- Lint: `ruff check bytedesk_omnigent/integration_recommendations.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_recommendations.py`
  - `All checks passed!`
- Diff hygiene: `git diff --check`
  - clean

Full suite was not run because this is a surgical, deterministic catalog/API addition and the targeted integration-capability tests cover the touched router plus adjacent catalog/gap/matrix behavior.
