# Omnigent autonomous feature loop — iteration 8

## Capability shipped

Iteration 8 adds a JSON-payload webhook adapter for Omnigent's signed ingress seam.

Prior loop PRs already established the integration catalog, workflow planning, app manifests, and several provider-specific webhook adapters. This iteration avoids duplicating those branches by adding a generic capability for work-management systems that carry the event type in the JSON body rather than a dedicated event header.

The new `JsonPayloadWebhookAdapter`:

- keeps the existing HMAC-SHA256 verification contract (`X-Omnigent-Signature` / `X-Hub-Signature-256`), so it fits the current secret resolver and ingress route without new dependencies;
- derives the binding `match_key` from common payload fields: `event`, `type`, `webhookEvent`, `webhook_event`, `event_type`, `issue_event_type_name`, `action.type`, and `action`;
- honors `X-Omnigent-Event` as an explicit override for relays that can stamp a canonical event name;
- registers built-in source aliases for `json`, `jira`, `trello`, `asana`, and `notion`.

Example binding flow:

1. Configure `OMNIGENT_INGRESS_SECRET_JIRA`.
2. Register a binding from `source=jira`, `match_key=jira:issue_updated`, to a parked signal such as `issue:OPS-42`.
3. POST a signed Jira-style JSON webhook to `/v1/ingress/jira` with `{ "webhookEvent": "jira:issue_updated", ... }`.
4. Omnigent resolves the payload event, delivers the durable signal, and wakes the waiting agent run.

## Files changed

- `bytedesk_omnigent/ingress.py`
  - Extends the webhook adapter protocol so match-key derivation can inspect both the raw body and headers.
  - Adds `JsonPayloadWebhookAdapter` and a small dotted-path JSON extractor.
  - Registers JSON/work-management aliases in the webhook source registry.
- `tests/ingress/test_ingress.py`
  - Updates adapter tests for the raw-body-aware protocol.
  - Adds unit coverage for payload-field match-key extraction.
  - Adds an end-to-end ingress test showing a Jira-style payload waking a bound signal.

## Business case

This broadens Omnigent's third-party application surface without adding one-off route code for every SaaS vendor. Work-management and collaboration tools are high-frequency automation entry points: ticket updates, card moves, page changes, and task completions are exactly the external signals that should wake autonomous agents.

A generic signed JSON adapter lets ByteDesk users integrate with Jira/Trello/Asana/Notion-style systems quickly, while preserving the safe ingress properties already established by earlier loop work: signed requests, exact binding resolution, durable signal delivery, replay handling, and non-2xx failures when no agent is actually woken.

## Future unlocks

- Add per-source configurable JSON event paths through the integration capability catalog once that PR lands.
- Add provider-specific adapters for services whose signatures are not HMAC-SHA256.
- Expose adapter names and expected event fields through `/v1/integration-capabilities` so ByteDesk Platform can render setup instructions.
- Generate sample webhook binding manifests from connected-app manifests.
- Add OAuth-backed setup flows that create SaaS webhook subscriptions automatically instead of asking operators to configure them manually.

## Verification

Targeted verification was run for the changed surface:

- `pytest tests/ingress/test_ingress.py`
- `ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py`
