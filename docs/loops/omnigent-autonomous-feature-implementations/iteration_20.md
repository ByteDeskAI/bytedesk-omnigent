# Omnigent autonomous feature loop — iteration 20

## Capability shipped

Iteration 20 adds a deterministic connected-app activation gate compiler for ByteDesk Platform.

New surface:

- `POST /v1/integration-activation-gates/compile`
- Pure compiler module: `bytedesk_omnigent.integration_activation_gates`
- Route module: `bytedesk_omnigent.routes.integration_activation_gates`

The compiler turns a provider, workspace, connected-app id, desired capabilities, and setup checks into a stable activation decision:

- Stable activation id: `integration-activation:v1:{provider}:{workspace_id}:{connected_app_id}`.
- Canonical provider and capability slugs.
- Required gates derived from the connector capabilities.
- `ready` versus `blocked` status and a boolean `can_enable` flag.
- Ordered blockers with operator-facing remediation reasons.
- A deterministic Archon-style workflow sequence for connector rollout.

## Prior loop awareness

Before selecting this capability, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #96 / iteration 1: integration capability catalog.
- #98 / iteration 2: external work-item intake.
- #99 / iteration 3: integration workflow plan compiler.
- #100 / iteration 4: connected-app manifest compiler.
- #101 / iteration 5: Slack webhook ingress adapter.
- #102 / iteration 6: Stripe webhook ingress adapter.
- #103 / iteration 7: GitHub webhook event routing.
- #104 / iteration 8: JSON payload webhook adapter.
- #105 / iteration 9: integration approval plan compiler.
- #106 / iteration 10: Microsoft Teams webhook ingress adapter.
- #107 / iteration 11: Linear webhook ingress adapter.
- #108 / iteration 12: Shopify webhook ingress adapter.
- #109 / iteration 13: webhook binding management API.
- #110 / iteration 14: integration event route compiler.
- #111 / iteration 15: integration secret readiness plans.
- #112 / iteration 16: integration OAuth state tokens.
- #113 / iteration 17: webhook ingress preflight preview.
- #114 / iteration 18: integration replay plan compiler.
- #115 / iteration 19: integration handoff package compiler.

This iteration avoids duplicating provider-specific adapters, manifests, workflow plans, approval plans, OAuth state, secret readiness, preflight, replay, and handoff package work. It adds the missing final rollout gate that composes those artifacts into a single Platform-safe decision: can this connected app be enabled for live autonomous delivery now?

## Implementation details

- `compile_integration_activation_gate()` is pure and deterministic: it performs no network calls, reads no secrets, and is safe to call from setup wizards or operator dashboards.
- Capabilities determine required gates:
  - `webhook` requires `secret_ready`, `webhook_preview_passed`, and `route_configured`.
  - `oauth` requires `oauth_ready`.
  - `writeback` requires `replay_plan_ready` and `approval_policy_ready`.
  - `agent_handoff` requires `agent_handoff_ready`.
- Capability names accept common slug variants such as `agent_handoff` and `agent-handoff`.
- Check values accept booleans and operator-friendly strings such as `ready`, `passed`, `enabled`, and `ok`.
- The ByteDesk extension now mounts the activation-gate router alongside the existing ingress/goals/governance/task routes.

## Business case

Third-party integrations only create durable business value when customers trust the activation path. Prior loop branches added the pieces needed for connected-app onboarding and safe autonomous execution: capability cataloging, manifests, webhook adapters, binding, preflight, OAuth, secret readiness, replay plans, approval plans, and handoff packages. Platform still needed one deterministic decision artifact that says whether all required pieces are ready before flipping live delivery on.

The activation gate gives ByteDesk Platform a product-ready contract for connector setup screens, admin review, support escalation, and future marketplace certification:

1. Which gates are required for this connector's capabilities?
2. Which gates are already ready?
3. Which exact blocker prevents live autonomous delivery?
4. What should the operator or customer do next?
5. Can Platform safely enable live delivery without accidental agent wakeups or unapproved provider writeback?

That directly advances Omnigent's mission around autonomous agent management, coordination, third-party application integration, and ByteDesk Platform integration.

## Future unlocks

- Feed activation-gate output into a Platform connector wizard's final “Enable integration” button.
- Compose outputs from the open loop PRs automatically once they land: secret readiness, OAuth state, preflight preview, replay plan, approval plan, and handoff package.
- Persist activation decisions as connector audit records.
- Add provider-specific mandatory gates from the integration capability catalog after it lands on develop.
- Use the activation id as the shared trace id across connector setup, webhook delivery, agent dispatch, writeback, and support review.

## Test plan

TDD was used. The first targeted pytest run failed during collection because `bytedesk_omnigent.integration_activation_gates` did not exist yet:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_activation_gates.py -q`

After implementation, the same targeted test exposed an implementation bug where `agent_handoff` was normalized to `agent-handoff`, so the handoff gate was skipped. I fixed the slug normalization and reran the targeted tests.

Verified after implementation:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_activation_gates.py -q`
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_activation_gates.py bytedesk_omnigent/routes/integration_activation_gates.py bytedesk_omnigent/extension.py tests/bytedesk_omnigent/test_integration_activation_gates.py`
- `git diff --check`

Targeted scope only: this change adds one pure compiler, one thin FastAPI route, extension router registration, focused tests, and loop documentation. A full repository test suite was not run because the changed surface is surgical and the targeted pytest/lint/diff checks cover the new behavior.
