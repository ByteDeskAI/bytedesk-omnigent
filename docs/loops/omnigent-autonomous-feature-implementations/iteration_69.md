# Autonomous feature loop iteration 69 — integration autonomy policies

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_69`

## Capability delivered

Iteration 69 adds deterministic, credentialless autonomy policy compilation for every integration capability in the catalog, exposed through:

- `GET /v1/integration-capabilities/{slug}/autonomy-policy`

The new policy surface turns a catalog blueprint into a default operating boundary for autonomous agents: risk tier, autonomy level, approval requirement, read/write scope split, allowed actions, approval-required actions, forbidden actions, and rationale. It is intentionally pure metadata: no provider network calls, no OAuth exchanges, no secret reads, and no database migration.

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs whose heads match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open loop work already covers provider webhook ingress adapters, OAuth helpers, approval plans, activation gates, replay/rollback/rate-limit/retry/idempotency/backfill/rotation artifacts, readiness assessments, dependency graphs, risk registers, cutover checklists, marketplace listings, staffing plans, demo scenarios, sandbox fixtures, consent manifests, and verification evidence assessment.

This iteration avoids duplicating those PRs. Instead of adding another provider adapter or rollout checklist, it adds the missing runtime policy contract that tells Omnigent and ByteDesk Platform how much autonomy an agent should receive for a selected integration before live credentials or workflow phases are activated.

## Implementation description

- Added `bytedesk_omnigent.integration_autonomy_policy`.
  - Defines typed `IntegrationAutonomyPolicy` output.
  - Derives `internal_harness`, `external_read`, and `external_write` risk tiers from the existing integration capability catalog.
  - Splits catalog scopes into read and write scopes using deterministic, conservative heuristics.
  - Assigns one of three autonomy levels:
    - `deterministic_internal` for Archon-style workflow harnesses with no provider scopes.
    - `observed_external_read` for read-only external integrations.
    - `supervised_external_write` for connectors that can mutate provider-side state.
  - Emits allowed actions, approval-required actions, forbidden actions, and rationale tailored by integration category.
- Extended `bytedesk_omnigent.routes.integration_capabilities` with:
  - `GET /integration-capabilities/{slug}/autonomy-policy`
  - existing authenticated/local-mode route behavior through `require_user(...)`
  - deterministic 404 handling for unknown catalog slugs.
- Updated `omnigent/server/API.md` with the new endpoint contract.
- Added `tests/bytedesk_omnigent/test_integration_autonomy_policy.py` covering internal workflow harnesses, external write connectors, unknown slug handling, and API route exposure.

## Business case

Customers will not let autonomous agents operate inside Slack, Google Workspace, GitHub, CRMs, support desks, or commerce systems unless Omnigent can clearly state what those agents may do without approval, what needs human sign-off, and what is forbidden. This policy compiler makes that boundary available as a stable API contract that Platform UI, marketplace setup flows, and autonomous integration builders can consume.

That directly improves Omnigent's mission as agent middleware: it coordinates agent autonomy across third-party integrations while preserving trust, least privilege, and operator control. It also gives ByteDesk Platform a simple way to render safe defaults before activation rather than hard-coding provider-specific policy copy in the UI.

## Future unlocks

1. Platform admin UI can render autonomy badges and approval warnings next to every integration capability.
2. Agent launchers can attach the compiled policy to generated Tasks so downstream workers know their default autonomy boundary.
3. OAuth activation can block if live provider scopes exceed the policy's catalog-derived write scopes.
4. Verification evidence assessments can require an accepted autonomy policy before production rollout.
5. Marketplace listings can advertise the default autonomy mode for Slack, Google Workspace, GitHub, Linear/Jira, Notion, CRM/support, commerce, and workflow-harness connectors.

## Test plan

TDD RED/GREEN cycle from the managed iteration worktree:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_autonomy_policy.py -q
```

RED result: failed during collection with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_autonomy_policy'`.

GREEN result after implementation: `4 passed, 1 warning`.

Additional targeted verification run before PR:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_autonomy_policy.py tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py -q
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/ruff check bytedesk_omnigent/integration_autonomy_policy.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_autonomy_policy.py
PYENV_VERSION=system git diff --check
```

Results:

- Targeted pytest: `14 passed, 1 warning`
- Ruff: `All checks passed!`
- `git diff --check`: passed with no output

Full-suite pytest is intentionally skipped for this surgical, read-only metadata/API addition; the targeted suite covers the new compiler, the mounted route, and neighboring integration catalog/matrix behavior.
