# Iteration 53: Integration idempotency key compiler

## Capability shipped

Added a deterministic, secret-free idempotency key compiler for connected-app integration events:

- `bytedesk_omnigent.integration_idempotency_key.IntegrationIdempotencyKey`
- `bytedesk_omnigent.integration_idempotency_key.build_integration_idempotency_key(...)`

Given a provider source, event name, request headers, and parsed payload, Omnigent now returns a stable claim target:

- `scope`: `integration:{source}:{event}` with normalized source slugs.
- `key`: provider delivery ID, payload object ID, or canonical payload SHA-256 fallback.
- `strategy`: which deterministic strategy produced the key.
- `source` and `event`: normalized routing dimensions for storage/debugging.

The compiler deliberately does not copy arbitrary headers. It only reads an allowlist of delivery/correlation headers, so authorization tokens, cookies, and webhook signatures are not echoed into durable idempotency rows or task context.

## Prior loop awareness

Before choosing this feature, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- The integration capability catalog and connected-app manifest/workflow compilers.
- Provider webhook adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry.
- OAuth authorize/refresh/scope review, activation gates, replay/rollback/rate-limit/dead-letter helpers, credential rotation, task briefs, blueprint previews, and integration event envelopes.

This iteration does not add another provider adapter, OAuth helper, event envelope, or replay/dead-letter plan. It adds the missing cross-provider claim-key primitive that those adapters and future ByteDesk Platform ingress routes can use before dispatching autonomous agent work.

## Implementation details

Files changed:

- `bytedesk_omnigent/integration_idempotency_key.py`
  - Adds a frozen `IntegrationIdempotencyKey` dataclass.
  - Adds `build_integration_idempotency_key(...)`, a pure deterministic compiler.
  - Normalizes source names such as `Microsoft Teams` to `microsoft-teams`.
  - Builds scopes as `integration:{source}:{event}`.
  - Prefers known provider delivery headers for retry-stable keys.
  - Falls back to common payload identifiers (`id`, `event.id`, `data.id`, etc.).
  - Falls back to SHA-256 of canonical JSON over source, event, and payload.
  - Avoids copying sensitive headers into durable idempotency material.
- `tests/bytedesk_omnigent/test_integration_idempotency_key.py`
  - Covers provider delivery header preference and secret-header exclusion.
  - Covers payload ID fallback.
  - Covers canonical payload hash stability across reordered JSON dictionaries.

## Business case

Third-party apps retry webhooks, queue workers redeliver messages, and platform integrations can race during failover. Without a provider-neutral idempotency key, autonomous agents can create duplicate tickets, duplicate CRM updates, repeated customer replies, repeated refunds, or conflicting ByteDesk Platform actions.

This capability gives Omnigent a safe, reusable claim target for exactly-once integration work:

1. Verify and normalize the incoming provider event.
2. Compile a stable `(scope, key)` pair.
3. Claim it in the existing durable idempotency store.
4. Dispatch the agent task only when the claim wins.

That lowers operational risk for customer-facing connected-app agents and makes integrations easier to certify for production/marketplace use.

## Future unlocks

- Wire this compiler into `/v1/ingress/{source}` once the open provider-adapter and event-envelope PRs land.
- Store the selected `strategy` alongside idempotency rows for support/debug dashboards.
- Extend provider delivery header coverage as new catalog integrations are added.
- Let deterministic Archon-style workflow harness nodes use this claim key before running side-effecting steps.
- Expose a preview endpoint so ByteDesk Platform can show which idempotency key would be claimed for a sample webhook fixture.

## Test plan

TDD red phase:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_idempotency_key.py -q`
  - Failed as expected with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_idempotency_key'`.

Verification run:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_idempotency_key.py -q`
  - Passed: `3 passed, 1 warning in 0.12s`.
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_idempotency_key.py tests/ingress/test_ingress.py -q`
  - Passed: `10 passed, 1 warning in 0.77s`.

Full suite was not run because this is a surgical pure-helper addition with targeted coverage plus adjacent ingress regression coverage.
