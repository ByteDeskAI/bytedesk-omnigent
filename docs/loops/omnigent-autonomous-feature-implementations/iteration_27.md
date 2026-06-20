# Iteration 27 — HubSpot webhook ingress adapter

## Capability shipped

Added a built-in HubSpot webhook source adapter for Omnigent ingress. Omnigent can now accept HubSpot workflow/CRM webhook callbacks through the existing `/v1/ingress/{source}` path using `source=hubspot`, verify the HubSpot legacy signature, extract the HubSpot event type from the raw JSON payload, resolve an existing webhook binding, and wake a parked Omnigent session through the durable signal bus.

This is intentionally surgical: it extends the existing webhook adapter registry rather than introducing a new route, store, secret system, or background worker.

## Prior loop awareness

Before selecting this work, I inspected open loop PRs with `gh pr list --repo ByteDeskAI/bytedesk-omnigent --state open` and avoided duplicated work:

- Iterations 5-8, 10-12, 23-26 already cover Slack, JSON payload ingress, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, and Asana webhook adapters.
- Iterations 13-22 cover binding management, route/replay/approval/secret readiness, OAuth state/authorize URL, activation gates, handoff packages, and deterministic workflow harness compilation.
- HubSpot was not represented among the open loop PRs, and is a high-value CRM integration for autonomous follow-up, support escalation, lifecycle marketing, and sales operations agents.

## Implementation details

Files changed:

- `bytedesk_omnigent/ingress.py`
  - Added `HubSpotWebhookAdapter`.
  - Registers `hubspot` in the existing webhook adapter registry.
  - Evolves adapter routing so body-aware adapters can derive a match key from the raw request body while preserving compatibility for existing header-only adapters/plugins.
  - HubSpot verification uses the legacy HubSpot webhook signature contract: `X-HubSpot-Signature = sha256(clientSecret + rawBody)`.
  - HubSpot match-key extraction reads the first event object from the JSON body and prefers `subscriptionType`, then `eventType`, `event_type`, then `type`, falling back to `*` for catch-all bindings.

- `tests/ingress/test_ingress.py`
  - Adds regression coverage for signature verification and body-derived `subscriptionType` routing.
  - Verifies `resolve_webhook_adapter("hubspot")` returns the built-in HubSpot adapter.

## Business case

HubSpot is a common system of record for SMB and mid-market sales/support teams. This capability lets ByteDesk/Omnigent agents react to CRM events such as contact property changes, deal stage changes, ticket updates, or lifecycle events without polling or brittle custom glue.

Examples unlocked:

- Wake a sales follow-up agent when a HubSpot deal reaches a target stage.
- Trigger a support escalation agent when a HubSpot ticket changes priority.
- Start an onboarding agent when a contact lifecycle stage changes.
- Route marketing operations tasks from HubSpot workflows into Omnigent-managed autonomous runs.

Because the adapter feeds the existing durable signal bus, these workflows inherit Omnigent's current idempotency, binding, replay, and parked-session behavior.

## Future unlocks

- Add HubSpot v3 signature verification once the ingress adapter contract includes method + full URI canonicalization inputs.
- Add a `/v1/integration-capabilities` catalog entry or manifest entry for HubSpot when that catalog is present on the branch.
- Add a HubSpot OAuth connection plan using the already-open OAuth authorize/state loop work after those PRs land.
- Add UI affordances for creating `hubspot` webhook bindings from CRM event names.

## Test plan

RED:

- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_hubspot_adapter_verifies_legacy_signature_and_extracts_subscription_type tests/ingress/test_ingress.py::test_hubspot_source_is_registered_by_default -q`
- Failed before implementation with `ImportError: cannot import name 'HubSpotWebhookAdapter'`.

GREEN / verification:

- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q`
  - Result: `9 passed, 1 warning in 0.73s`.
- `/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py`
  - Result: `All checks passed!`.

Full-suite note: this iteration is limited to the ingress adapter seam and its existing unit-test file; the full test suite is much broader and includes slow/e2e harness tests, so targeted ingress tests plus targeted ruff were used.
