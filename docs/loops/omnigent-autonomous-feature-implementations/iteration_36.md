# Iteration 36 — Airtable webhook ingress adapter

## Capability shipped

Added a first-party `airtable` webhook source adapter to ByteDesk Omnigent's inbound event ingress. Agents can now bind durable signals to Airtable change notifications without needing a custom deployment-side adapter.

The adapter:

- verifies Airtable-style HMAC-SHA256 webhook signatures via `X-Airtable-Webhook-Signature`;
- accepts the same bare hex and `sha256=<hex>` digest forms used by existing ingress verification;
- preserves an `X-Omnigent-Event` override for deterministic tests/manual routing;
- derives deterministic binding keys from Airtable notification payloads:
  - `base.changed` when the payload contains base metadata;
  - `webhook.changed` when webhook metadata is present without base metadata;
  - `*` for catch-all bindings;
- registers the adapter by default under the `airtable` source name.

## Prior loop awareness

Before selecting the feature, I inspected open loop PRs matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Existing open iterations already cover Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, generic HMAC/JSON webhook seams, OAuth/state/activation/approval/replay/rollback/task-brief compilers, and the webhook adapter manifest. Airtable was not represented in the open loop PR list, so this iteration adds a non-duplicative service integration.

## Implementation details

- `bytedesk_omnigent/ingress.py`
  - Added `AirtableWebhookAdapter` implementing the existing `WebhookSourceAdapter` contract.
  - Extended `match_key` to optionally accept parsed payload data so adapters can derive route keys from JSON bodies when a provider does not provide a stable event header.
  - Kept backward compatibility for deployment-registered adapters by falling back to the older one-argument `match_key(headers)` call when an adapter does not accept `payload=`.
  - Registered `airtable` in the webhook source adapter registry.

- `tests/ingress/test_ingress.py`
  - Added TDD coverage for Airtable HMAC verification, payload-derived route keys, and default registry resolution.

## Business case

Airtable is a common lightweight CRM, operations database, support queue, and content pipeline for small teams. Native Airtable webhook ingress lets Omnigent agents react to table/base changes and trigger follow-up work in ByteDesk Platform: create tasks, route customer updates, wake specialist agents, or coordinate approval workflows. This expands the integration surface for non-technical teams that already use Airtable as their source of operational truth.

## Future unlocks

- Add an Airtable event normalization layer that converts payload deltas into richer agent task briefs.
- Expose Airtable binding templates in the integration capability catalog once the catalog PR lands.
- Add an OAuth/app-install flow for Airtable connected apps so users can create webhooks and secrets from ByteDesk Platform.
- Add per-table binding keys such as `table.<tbl_id>.changed` after payload examples are validated against production Airtable webhook notifications.

## Test plan

Targeted verification run in the iteration 36 worktree:

- RED: `pytest tests/ingress/test_ingress.py::test_airtable_adapter_verifies_hmac_and_derives_event_from_payload tests/ingress/test_ingress.py::test_airtable_source_is_registered_by_default -q` failed with `ImportError: cannot import name 'AirtableWebhookAdapter'` before implementation.
- GREEN: `pytest tests/ingress/test_ingress.py::test_airtable_adapter_verifies_hmac_and_derives_event_from_payload tests/ingress/test_ingress.py::test_airtable_source_is_registered_by_default -q` passed.
- Regression: `pytest tests/ingress/test_ingress.py -q` passed (`9 passed`, one pre-existing known-failures warning from `tests/conftest.py`).

Full suite was not run because this is a surgical ingress-adapter change; targeted ingress tests cover the touched code path.
