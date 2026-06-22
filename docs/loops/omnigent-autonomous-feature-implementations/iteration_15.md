# Omnigent autonomous feature loop — iteration 15

## Capability shipped

Iteration 15 adds a deterministic integration secret readiness plan compiler for connected-app onboarding.

New surface:

- `POST /v1/integration-secret-plans/compile`
- Pure compiler module: `bytedesk_omnigent.integration_secret_plans`
- Route module: `bytedesk_omnigent.routes.integration_secret_plans`

The compiler turns a provider/workspace request into a previewable credential readiness plan containing:

- Canonical provider and ingress source slug.
- Required environment variable names for OAuth clients, webhook signing secrets, account subdomains, verification tokens, public keys, or bot tokens.
- Provider OAuth scopes, with writeback scopes only when requested.
- Recommended webhook/event match keys to bind to parked Omnigent signals.
- Human approval gates for connected-app install and autonomous writeback.
- Deterministic provisioning steps that Platform or operator tooling can render before enabling an integration.
- Verification metadata such as secret env var prefix, ingress URL template, dry-run probe, and stable idempotency key.

Seeded providers:

- HubSpot
- Salesforce
- Zendesk
- Intercom
- Google Workspace
- Airtable
- Discord

## Prior loop awareness

Before choosing this capability, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #96 / iteration 1: integration capability catalog.
- #98 / iteration 2: external work-item intake.
- #99 / iteration 3: deterministic integration workflow plan compiler.
- #100 / iteration 4: connected-app installation manifest compiler.
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

This iteration avoids duplicating those open loop branches. Instead of adding another webhook adapter, event route compiler, manifest compiler, or approval compiler, it fills the operational readiness gap between a planned integration and a safely activated integration: knowing exactly which secrets, scopes, match keys, and verification checks must exist before ByteDesk Platform enables third-party events to wake Omnigent agents.

The canonical checkout had unrelated uncommitted work, so the managed worktree command initially refused to create the branch. I reran the same managed operator with `--allow-dirty`, leaving canonical WIP untouched, and verified the resulting branch was exactly `feature/loop/omnigent-autonomous-feature-implementations/iteration_15`.

## Implementation details

- Added `RequiredSecret`, `ProvisioningStep`, and `IntegrationSecretPlan` dataclasses with a `to_dict()` serializer for route responses.
- Added provider blueprints for HubSpot, Salesforce, Zendesk, Intercom, Google Workspace, Airtable, and Discord.
- Added provider alias normalization, including `google-workspace` -> `google_workspace`.
- Added fail-closed validation for missing provider, missing workspace id, and unsupported providers.
- Added `requested_events` support so Platform can narrow recommended match keys to events selected during onboarding.
- Added `writeback` support so write scopes and the `approve_autonomous_writeback` gate are included only for integrations that will mutate third-party systems.
- Registered the route through `BytedeskExtension.routers()` so Omnigent exposes it under `/v1` with the same optional auth-provider pattern as sibling ByteDesk routes.

## Business case

Third-party integrations fail or become risky when credential setup is ad hoc. Before an autonomous agent can safely act in HubSpot, Salesforce, Zendesk, Intercom, Google Workspace, Airtable, Discord, or similar systems, ByteDesk Platform needs a deterministic checklist of secrets, scopes, event match keys, and verification probes.

This capability turns integration setup into a productizable contract:

1. Platform asks Omnigent for a provider/workspace secret readiness plan.
2. The UI renders exact credential fields and OAuth scopes.
3. Operators or installers provision secrets under predictable names.
4. Platform binds recommended events to parked Omnigent signals.
5. Writeback is activated only after explicit approval gates are satisfied.

That shortens connector onboarding, reduces support burden, prevents partially configured integrations from silently dropping events, and gives future marketplace/agent developers a stable checklist they can use without knowing Omnigent internals.

## Future unlocks

- Feed the secret readiness plan into connected-app manifests so Platform can render one install wizard from manifest + approval + secret plan outputs.
- Add provider-specific verification probes that check configured env vars and OAuth grants without exposing secret values.
- Persist generated plans with installer identity for audit trails.
- Extend the blueprint table to Jira, Linear, Slack, Notion, Trello, Shopify, Stripe, Microsoft Teams, Asana, Monday, and GitHub once their open loop branches land.
- Use readiness plan completion as a precondition before webhook binding activation.
- Expose blueprint metadata through the integration capability catalog when that open branch lands.

## Test plan

Targeted tests and lint were run because this iteration touched a pure compiler, one thin route, extension route registration, docs, and focused unit/API coverage rather than the full runtime.

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_secret_plans.py -q`
  - Expected failure observed: `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_secret_plans'`.
- GREEN: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_secret_plans.py -q`
  - Result: `4 passed, 1 warning`.
- Lint: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_secret_plans.py bytedesk_omnigent/routes/integration_secret_plans.py bytedesk_omnigent/extension.py tests/bytedesk_omnigent/test_integration_secret_plans.py`
- Whitespace: `git diff --check`
