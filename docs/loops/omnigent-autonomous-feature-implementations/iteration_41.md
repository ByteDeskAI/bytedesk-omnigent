# Iteration 41: Salesforce webhook ingress adapter

## Capability shipped

Added a built-in Salesforce webhook source adapter for Omnigent ingress. The adapter lets Omnigent bind Salesforce-originated callbacks to durable signal waits using the existing `/v1/ingress/{source}` route and webhook binding store.

The shipped capability is intentionally surgical:

- Registers `salesforce` as a first-class webhook source adapter.
- Verifies `X-Salesforce-Signature` as a base64-encoded HMAC-SHA256 digest over the raw request body using the source secret resolved by Omnigent's existing secret resolver.
- Routes events by `X-Salesforce-Event` when a connector supplies it.
- Falls back to CloudEvents `ce-type` for Salesforce Event Relay style deliveries.
- Falls back to `*` so existing catch-all bindings still work.

## Prior loop awareness

Before selecting the feature, I inspected open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open prior work already covered Slack, Stripe, GitHub routing, Microsoft Teams, Linear, Shopify, Jira, Zendesk, Asana, HubSpot, Intercom, GitLab, Airtable, Google Workspace, CloudEvents, Monday, ServiceNow, and several deterministic integration compilers/harnesses.

Salesforce was still not represented in the open loop PR set, and it is one of the requested high-value service integrations. This PR builds on the existing adapter seam instead of adding a parallel route or new storage model.

## Implementation details

Changed files:

- `bytedesk_omnigent/ingress.py`
  - Added `SalesforceWebhookAdapter`.
  - Registered it under source name `salesforce` in the existing `webhook_source` pluggable registry.
  - Kept GitHub as the default adapter for unspecified sources.

- `tests/ingress/test_ingress.py`
  - Added a TDD coverage case proving the Salesforce adapter resolves from the registry, validates base64 HMAC signatures, rejects missing/invalid signatures, and extracts event routing keys from Salesforce and CloudEvents headers.

No secrets, environment files, deployment manifests, or unrelated user work were modified.

## Business case

Salesforce is a core system of record for sales, support, and revenue operations. A native Salesforce ingress adapter lets Omnigent agents react to account, opportunity, case, and platform-event changes without polling or brittle custom glue.

This directly supports Omnigent's mission by allowing autonomous agents to be embedded into third-party business workflows:

- Wake a sales operations agent when a high-value opportunity changes stage.
- Trigger account research or renewal-risk analysis when Salesforce emits account updates.
- Route support escalations from Salesforce cases to specialized Omnigent agents.
- Coordinate cross-system automations that start in Salesforce and continue through ByteDesk Platform or other integrations.

## Future unlocks

- Add a Salesforce connected-app manifest/OAuth capability that compiles required scopes and callback URLs.
- Add typed event mapping helpers for common Salesforce events such as `OpportunityChangeEvent`, `CaseChangeEvent`, and `AccountChangeEvent`.
- Expose Salesforce in an integration-capability catalog endpoint once the catalog lands on develop.
- Add an end-to-end ingress route test once the open webhook adapter PR stack lands and route fixtures stabilize.

## Test plan

Red/green TDD evidence:

1. Added `test_salesforce_adapter_verifies_base64_hmac_and_reads_event_headers` before implementation.
2. Ran the targeted test and observed the expected import failure because `SalesforceWebhookAdapter` did not exist yet.
3. Implemented the adapter and registry registration.
4. Re-ran the targeted test successfully.

Verification performed:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_salesforce_adapter_verifies_base64_hmac_and_reads_event_headers -q`
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress -q`
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py`
- `git diff --check`

Full suite was not run because this is a surgical ingress-adapter change; targeted ingress tests cover the changed behavior and adjacent ingress semantics.
