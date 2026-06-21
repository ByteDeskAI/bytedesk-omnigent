# Iteration 80 — Integration tool contract compiler

## Capability shipped

Iteration 80 adds a deterministic integration tool contract compiler for catalog-backed third-party integrations:

- Pure compiler: `bytedesk_omnigent.integration_tool_contracts.compile_integration_tool_contract`
- API route: `GET /v1/integration-capabilities/{slug}/tool-contract`
- Targeted tests for both compiler behavior and route serialization

For any capability in `/v1/integration-capabilities`, the compiler returns the least-privilege agent tool surface Omnigent should expose before allowing an autonomous agent to operate that integration. The contract includes stable tool names, required inputs, required OAuth scopes, approval requirements, category policy gates, and agent-blueprint hints.

## Prior loop awareness

Before choosing this scope, I inspected open PRs whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Existing open loop work already covers integration capability catalogs, workflow plans/harnesses, connected-app manifests, webhook adapters, OAuth helpers, approval/replay/handoff/activation/readiness/risk/dependency/demo/staffing/marketplace/recommendation/evidence/routing/gap/acceptance/redaction/telemetry surfaces.

This iteration intentionally avoids adding another webhook adapter, OAuth flow, verification matrix, gap analysis, acceptance suite, telemetry contract, pilot/cutover plan, or marketplace artifact. Instead it fills a narrower execution gap: turning a catalog capability into the exact agent-callable tool contract needed to create safe specialist agents and ByteDesk Platform integration UI.

## Implementation details

### `bytedesk_omnigent.integration_tool_contracts`

New frozen dataclass:

- `IntegrationToolContract`

New public function:

- `compile_integration_tool_contract(slug: str) -> dict | None`

The compiler uses the existing catalog and verification matrix to derive:

- `risk_tier` from the verification matrix
- category-specific policy gate ids such as `communication-loop`, `developer-change-safety`, and `workflow-determinism`
- deterministic tool names from the capability slug
- read-only context tools for all capabilities
- event normalization tools for all capabilities
- write execution tools only for `external_write` capabilities
- evidence recording tools for all capabilities

Write tools require `approval_id`, `dry_run`, and `idempotency_key` inputs so generated agents cannot jump straight from external context to provider mutation without an auditable approval path.

### Route extension

`bytedesk_omnigent.routes.integration_capabilities` now exposes:

```text
GET /v1/integration-capabilities/{slug}/tool-contract
```

The endpoint is read-only, uses the same auth boundary as sibling integration capability routes, returns `404` for unknown capability slugs, and performs no network calls or secret reads.

## Business case

Omnigent's mission depends on safely creating and managing agents that can operate inside third-party applications. Catalog entries describe what integrations are valuable, and verification matrices describe how to prove rollout safety. Platform builders still need a contract for what tools a generated specialist agent is allowed to receive.

This feature gives ByteDesk Platform and future agent factories a deterministic bridge from product roadmap capability to least-privilege tool grants. It helps answer:

- Which tools should a Slack, GitHub, Google Workspace, or Archon-style workflow agent receive?
- Which tools are read-only versus mutating?
- Which OAuth scopes back each tool?
- Which policy gate controls the tool?
- Which inputs prove approval, dry-run, idempotency, and evidence capture?

That shortens the path from catalog capability to safely generated revenue-producing integration agents.

## Future unlocks

- Render tool-contract cards in ByteDesk Platform when admins configure connected apps.
- Feed the contract into YAML agent-spec generation so specialist agents get scoped tools automatically.
- Bind `category_policy_gate` to runtime policy checks before exposing mutating provider tools.
- Persist contract versions with connected-app installs for drift detection.
- Add provider-specific MCP adapter generation once real OAuth connectors land.

## Verification

Targeted tests and lint run from the iteration 80 worktree:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_tool_contracts.py -q
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_tool_contracts.py -q
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_tool_contracts.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_tool_contracts.py
git diff --check
```

Results:

- `4 passed, 1 warning in 0.15s`
- `14 passed, 1 warning in 0.18s`
- `ruff`: `All checks passed!`
- `git diff --check`: passed with no output

The warning is the repository's existing `tests/known_failures.yaml` unmatched-entry warning emitted by `tests/conftest.py`; it is unrelated to this change.
