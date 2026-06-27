# Omnigent autonomous feature loop iteration 88

## Capability shipped

Iteration 88 adds deterministic integration agent prompt packs. The new compiler turns any cataloged integration capability into a JSON-ready agent creation payload containing:

- a provider-aware role name,
- an autonomy mode derived from the verification matrix risk tier,
- required OAuth/service scopes,
- allowed actions and blocked actions,
- future success outcomes from the catalog,
- verification gate ids, and
- a complete system prompt that embeds rollout gates and required evidence.

The capability is exposed through:

- `compile_integration_agent_prompt_pack(slug)` in `bytedesk_omnigent/integration_agent_prompt_pack.py`
- `GET /v1/integration-capabilities/{slug}/agent-prompt-pack`

This directly enhances Omnigent's mission by making catalog integrations usable as deterministic agent-creation inputs rather than only product metadata.

## Prior loop awareness

Before selecting this feature, I inspected open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Recent open work already covers SLO profiles, deprecation plans, ownership matrices, data boundary manifests, evidence assessments, remediation playbooks, coordination topologies, tool contract compilers, telemetry contracts, value scorecards, redaction profiles, acceptance suites, pilot plans, gap analysis, tenant routing, evidence packets, recommendations, incident drills, autonomy policies, verification assessments, consent manifests, sandbox fixtures, cutover checklists, risk registers, dependency graphs, readiness assessments, demo scenarios, staffing plans, marketplace listings, retry/idempotency/event envelope compilers, OAuth scope review, agent blueprint previews, and many provider webhook adapters.

This iteration intentionally does not add another provider webhook adapter or another rollout checklist. It builds on the existing integration catalog and verification matrix to produce a concrete agent prompt pack that can be consumed by agent creation and ByteDesk Platform surfaces.

## Implementation details

- Added `bytedesk_omnigent/integration_agent_prompt_pack.py`.
  - Combines `get_integration_capability()` and `compile_integration_verification_matrix()`.
  - Maps risk tiers to deterministic autonomy modes:
    - `internal_harness` -> `deterministic_harness`
    - `external_read` -> `read_only_observer`
    - `external_write` -> `approval_gated_write`
  - Emits explicit allowed/blocked action boundaries per risk tier.
  - Preserves known brand styling for Archon-style workflow agents.
- Added `GET /v1/integration-capabilities/{slug}/agent-prompt-pack` to the integration capabilities router.
- Tightened integration verification matrix risk classification so scopes containing `update`, `insert`, `delete`, or `send` are treated as mutating external-write scope markers, not read-only access.
- Added targeted tests for direct compiler behavior, route behavior, missing slugs, Archon-style internal harness handling, Slack write-gated behavior, and Notion mutation scope handling.

## Business case

Customers do not only need a list of possible integrations; they need a safe path to turn those integrations into deployable specialist agents. Prompt packs convert roadmap metadata into agent-creation material that ByteDesk Platform can display, review, version, and eventually instantiate. This shortens the path from integration discovery to revenue-generating autonomous workflows while preserving policy boundaries and verification evidence.

## Future unlocks

- Feed prompt packs directly into hosted agent creation flows.
- Add downloadable YAML/JSON agent spec generation from each prompt pack.
- Add tenant-specific overlays for selected scopes, approval policy, and connector configuration.
- Pair prompt packs with sandbox fixtures so newly created integration agents can run deterministic dry-runs before touching provider systems.
- Surface prompt packs in ByteDesk Platform as a one-click "Create integration agent" action.

## Test plan

Targeted verification run from the managed iteration 88 worktree:

```bash
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_agent_prompt_pack.py tests/bytedesk_omnigent/test_integration_verification_matrix.py -q
```

Result: `8 passed, 1 warning in 0.16s`.

The warning is the repo's existing `tests/known_failures.yaml` unmatched-entry warning and is not introduced by this change.

Additional checks:

```bash
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_agent_prompt_pack.py bytedesk_omnigent/integration_verification_matrix.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_agent_prompt_pack.py
git diff --check
```
