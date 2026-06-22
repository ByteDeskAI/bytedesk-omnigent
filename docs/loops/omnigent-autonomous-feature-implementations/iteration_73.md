# Omnigent Autonomous Feature Loop Iteration 73

## Capability shipped

Added deterministic integration tenant routing manifests for the integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/tenant-routing-manifest`

New pure compiler:

- `bytedesk_omnigent.integration_tenant_routing.compile_integration_tenant_routing_manifest(slug)`

The manifest is secret-free and JSON-ready. It tells ByteDesk Platform, Omnigent operators, and future autonomous planning loops how a catalog capability should map provider workspaces, provider actors, and events back into Omnigent tenant boundaries and coordination queues before real credentials or webhooks are installed.

## Prior loop awareness

Before selecting this feature, I inspected open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*` using `gh pr list --repo ByteDeskAI/bytedesk-omnigent`.

Open loop work already covers provider ingress adapters, OAuth/approval/activation/readiness surfaces, workflow harness compilation, rollback/retry/rate-limit/dead-letter/backfill/idempotency planning, sandbox fixtures, risk/readiness/verification artifacts, recommendation compilers, and evidence packets through iteration 72.

This iteration intentionally avoids duplicating those. It fills the tenant/workspace routing gap: how each integration capability should preserve tenant identity, provider workspace identity, provider actor identity, default signal queues, isolation checks, and audit tags so ByteDesk Platform can safely expose integrations in a multi-tenant environment.

## Implementation details

Files changed:

- `bytedesk_omnigent/integration_tenant_routing.py`
  - Adds `TenantSignalRoute` and `compile_integration_tenant_routing_manifest`.
  - Classifies capabilities into `external_workspace_mapping` or `internal_workflow_namespace` routing modes.
  - Emits stable workspace identity fields:
    - External providers: `tenant_id`, `provider_workspace_id`, `provider_actor_id`
    - Internal workflow harnesses: `tenant_id`, `workflow_blueprint_id`, `workflow_run_id`
  - Emits category-specific default signal routes for communication, project management, knowledge, developer, CRM/support, commerce/billing, and workflow harness capabilities.
  - Emits tenant isolation checks and audit tags including capability slug, category, and risk tier.

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds authenticated read route for `{slug}/tenant-routing-manifest` alongside the existing catalog detail and verification matrix endpoints.

- `tests/bytedesk_omnigent/test_integration_tenant_routing.py`
  - Covers internal workflow harness manifests, external provider manifests, unknown slug behavior, and the FastAPI route.

## Business case

Omnigent's integration catalog becomes more platform-ready when every proposed connector also declares its tenant routing contract. This is critical for ByteDesk Platform adoption because customers need assurance that Slack workspaces, Linear/Jira projects, Notion workspaces, GitHub installations, and workflow harness runs cannot leak across tenants or route events to the wrong agent workforce.

The manifest also shortens sales and implementation cycles: platform UI can preview the routing contract before OAuth setup, solution engineers can explain tenant isolation without reading code, and autonomous setup agents can compile deterministic onboarding tasks from the same API.

## Future unlocks

- Tenant-aware integration setup wizards in ByteDesk Platform.
- Automated validation that webhook bindings include required provider workspace and actor identifiers.
- Queue provisioning based on catalog category and risk tier.
- Cross-tenant replay tests generated from each manifest's isolation checks.
- Marketplace trust badges showing that a connector declares tenant identity, isolation, and audit tags before activation.

## Test plan

TDD was followed:

1. Added `tests/bytedesk_omnigent/test_integration_tenant_routing.py` first.
2. Ran the new test and observed expected collection failure because `bytedesk_omnigent.integration_tenant_routing` did not exist.
3. Implemented the compiler and route.
4. Re-ran the targeted test and observed it pass.

Verification commands run:

- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_tenant_routing.py -q`

Targeted scope is appropriate because the change is a small pure compiler plus one existing router extension. The final PR verification also runs the neighboring integration catalog and verification matrix tests plus `git diff --check`.
