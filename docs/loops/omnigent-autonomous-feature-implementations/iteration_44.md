# Iteration 44: Deterministic OAuth Refresh Plan Compiler

## Capability shipped

Iteration 44 adds a deterministic OAuth token-refresh plan compiler for ByteDesk Omnigent connected apps.

The new `bytedesk_omnigent.oauth_refresh.compile_oauth_refresh_plan` function converts connected-app metadata into a JSON-serializable workflow plan that an Omnigent agent, background worker, or ByteDesk Platform control plane can execute safely. It produces stable provider/app identifiers, vault secret references, idempotency scope, refresh lock key, ordered execution steps, required scopes, and rollback behavior without reading secrets or calling third-party OAuth endpoints during planning.

## Prior loop awareness

Before choosing this capability, I inspected the open loop PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open loop work already covers the integration capability catalog, webhook ingress adapters for Slack, Stripe, GitHub, Teams, Linear, Shopify, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Airtable, Google Workspace, CloudEvents, Monday, ServiceNow, Salesforce, and several webhook/activation/approval/rollback/replay/rate-limit/dead-letter planning surfaces.

This iteration deliberately avoids adding another webhook adapter or duplicating OAuth authorize URL/state-token work. It targets the next operational gap after authorization: keeping connected apps alive through deterministic, auditable OAuth refresh orchestration.

## Implementation details

Changed files:

- `bytedesk_omnigent/oauth_refresh.py`
  - Adds `compile_oauth_refresh_plan`.
  - Normalizes provider names into deterministic slugs.
  - Validates required fields.
  - Deduplicates scopes while preserving first-seen order.
  - Emits lock and idempotency keys suitable for autonomous refresh executors.
  - Emits ordered refresh steps and rollback policy.

- `bytedesk_omnigent/__init__.py`
  - Exposes `compile_oauth_refresh_plan` from the ByteDesk extension package.

- `tests/integrations/test_oauth_refresh.py`
  - Covers connected-app plan shape.
  - Covers scope normalization/deduplication.
  - Covers required-field validation.

The compiler is intentionally side-effect-free. It does not touch secrets, does not mutate token state, and does not call provider APIs. That makes it safe to use inside planning, previews, PR-generated runbooks, deterministic workflow harnesses, or future API endpoints.

## Business case

OAuth refresh is the reliability backbone for integrations such as Slack, Notion, Trello, GitHub, Linear, Jira, Google Workspace, HubSpot, Salesforce, Zendesk, Intercom, Stripe, Shopify, Microsoft Teams, Discord, Asana, Monday, and Airtable.

Without a deterministic refresh plan, agents can be authorized once but later fail silently when access tokens expire. This compiler gives ByteDesk Omnigent a stable contract for keeping connected apps online while respecting secret isolation, idempotency, locks, and recovery paths.

This directly improves Omnigent's mission by making third-party application integration more durable and operator-friendly.

## Future unlocks

- Add a `POST /v1/integrations/{provider}/{connected_app_id}/oauth-refresh-plan` preview endpoint.
- Wire the plan into a deterministic workflow executor with the existing idempotency store and signal bus.
- Add provider-specific health probe adapters for Slack, Google Workspace, Linear, GitHub, and Microsoft Teams.
- Add scheduled proactive refresh before token expiry.
- Add dead-letter surfacing in ByteDesk Platform for failed refresh attempts.
- Attach refresh-plan previews to connected-app manifests and integration capability catalog responses.

## Test plan

Targeted TDD verification was used:

1. RED: `pytest tests/integrations/test_oauth_refresh.py -q` failed with `ModuleNotFoundError: No module named 'bytedesk_omnigent.oauth_refresh'`.
2. GREEN: implemented `bytedesk_omnigent/oauth_refresh.py` and exposed it from `bytedesk_omnigent/__init__.py`.
3. PASS: `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/integrations/test_oauth_refresh.py -q` passed: `3 passed, 1 warning`.

Additional verification:

- `git diff --check`
- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/oauth_refresh.py bytedesk_omnigent/__init__.py tests/integrations/test_oauth_refresh.py`
- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/integrations/test_oauth_refresh.py tests/ingress/test_ingress.py -q` passed: `10 passed, 1 warning`.
- targeted import/compile checks through pytest collection

Full-suite testing was not run because this is a surgical, pure-Python compiler with focused unit coverage and no runtime side effects.
