# Iteration 38: CloudEvents webhook ingress adapter

## Capability shipped

Added a built-in `CloudEventsWebhookAdapter` for standards-based webhook/event ingress. Sources that emit CloudEvents-style HTTP events can now authenticate with an `Authorization: Bearer TOKEN` header (or `X-Omnigent-Token` for emitters that cannot set Authorization) and route bindings by the standard `ce-type` header.

The adapter is registered under:

- `cloudevents`
- `salesforce`

This makes `source=salesforce` immediately usable for Salesforce Platform Events / event-relay style integrations without bespoke FastAPI route glue.

## Prior loop awareness

Before selecting this work, I inspected the open autonomous loop PRs for `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Existing open loop work already covers Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, the generic JSON/HMAC webhook adapters, OAuth/state/activation compilers, route/replay/rollback/probe compilers, and webhook adapter manifest surfaces.

To avoid duplicating those PRs, iteration 38 adds one new cross-provider ingress capability: a CloudEvents-native adapter that can unlock Salesforce and other event-bus integrations through the existing webhook binding flow.

## Implementation details

Changed `bytedesk_omnigent/ingress.py`:

- Added `CloudEventsWebhookAdapter` implementing the existing `WebhookSourceAdapter` protocol.
- Verification accepts either:
  - `Authorization: Bearer TOKEN`
  - `X-Omnigent-Token: TOKEN`
- Token comparison uses `hmac.compare_digest`.
- Routing uses the CloudEvents `ce-type` header as the binding `match_key`.
- Missing `ce-type` falls back to `*`, preserving the existing catch-all binding behavior.
- Registered the adapter in the webhook adapter registry as `cloudevents` and `salesforce`.

Changed `tests/ingress/test_ingress.py`:

- Added coverage for bearer-token verification, fallback token verification, failed-token rejection, `ce-type` extraction, and default `*` behavior.
- Added coverage that `resolve_webhook_adapter("salesforce")` returns the CloudEvents adapter.

## Business case

Salesforce is a high-value enterprise system for Omnigent customers. Many customer workflows start from CRM events: account updates, case escalations, opportunity stage changes, lead assignment, and renewal risk signals. A CloudEvents adapter lets Omnigent consume these events through the existing durable signal-bus/webhook-binding path instead of requiring a custom route per provider.

Because CloudEvents is also used by event gateways and cloud event routers, this single adapter broadens Omnigent's integration story beyond Salesforce: teams can put Eventarc, Event Grid, Knative, or internal ByteDesk emitters in front of SaaS events and route them into parked autonomous sessions deterministically.

## Future unlocks

- Add `eventarc`, `azure-event-grid`, and `knative` aliases once product wants them exposed explicitly in the adapter manifest/catalog.
- Add a binding preview endpoint that shows the derived CloudEvents `match_key` for a sample event body/header set.
- Add ByteDesk Platform UI affordances for selecting `salesforce` / `cloudevents` as an ingress source and mapping `ce-type` values to agent runs.
- Add optional CloudEvents JSON-body fallback for structured-mode events where HTTP headers are stripped by an intermediary.

## Verification

Targeted tests run:

```bash
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_cloudevents_adapter_verifies_bearer_token_and_reads_ce_type tests/ingress/test_ingress.py::test_salesforce_resolves_to_cloudevents_adapter -q
```

Result: `2 passed, 1 warning in 0.15s`.

The warning is the existing `tests/known_failures.yaml` unmatched-entry warning emitted by the test harness during targeted collection.
