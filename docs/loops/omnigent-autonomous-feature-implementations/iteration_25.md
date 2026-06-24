# Omnigent autonomous feature loop iteration 25

## Capability shipped

Added a built-in Zendesk webhook ingress adapter for Omnigent's signed webhook pipeline.

This lets teams connect Zendesk ticket events to parked Omnigent sessions without writing a custom adapter in their deployment. Zendesk can now be addressed as `POST /v1/ingress/zendesk`, with the existing secret resolver reading `OMNIGENT_INGRESS_SECRET_ZENDESK`, the existing binding store resolving `(source="zendesk", match_key=<event>)`, and the existing signal bus waking the waiting autonomous run.

## Prior loop awareness

Before selecting the feature, I inspected open loop PRs matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #96 iteration 1: integration capability catalog
- #98 iteration 2: external work item intake
- #99 iteration 3: integration workflow plan compiler
- #100 iteration 4: connected app manifest compiler
- #101 iteration 5: Slack webhook ingress adapter
- #102 iteration 6: Stripe webhook ingress adapter
- #103 iteration 7: GitHub webhook routing
- #104 iteration 8: JSON payload webhook adapter
- #105 iteration 9: integration approval plan compiler
- #106 iteration 10: Microsoft Teams webhook ingress adapter
- #107 iteration 11: Linear webhook ingress adapter
- #108 iteration 12: Shopify webhook ingress adapter
- #109 iteration 13: webhook binding management API
- #110 iteration 14: integration event route compiler
- #111 iteration 15: integration secret readiness plans
- #112 iteration 16: integration OAuth state tokens
- #113 iteration 17: webhook ingress preflight preview
- #114 iteration 18: integration replay plan compiler
- #115 iteration 19: integration handoff package compiler
- #116 iteration 20: integration activation gates
- #117 iteration 21: integration OAuth authorize URL compiler
- #118 iteration 22: integration workflow harness compiler
- #119 iteration 23: Discord ingress signature adapter
- #120 iteration 24: Trello webhook ingress adapter

Iteration 25 intentionally does not duplicate those open PRs. It adds Zendesk, a support/customer-success integration surface that was not present in the open loop work, while building on the existing pluggable webhook-source adapter seam in `bytedesk_omnigent.ingress`.

## Implementation details

Changed files:

- `bytedesk_omnigent/ingress.py`
  - Added `ZendeskWebhookAdapter`.
  - Verifies `X-Zendesk-Webhook-Signature` using HMAC-SHA256 over `timestamp + raw_body` and base64 encoding.
  - Requires `X-Zendesk-Webhook-Signature-Timestamp` so stale/malformed signed deliveries do not accidentally verify.
  - Uses `X-Omnigent-Event` as the routable match key, falling back to the existing `"*"` catch-all behavior.
  - Registers `zendesk` in the built-in webhook adapter registry.
- `tests/ingress/test_ingress.py`
  - Added tests for Zendesk signature verification, timestamp mismatch rejection, missing timestamp rejection, event extraction, catch-all fallback, and built-in registry resolution.

The adapter composes with existing ingress primitives instead of introducing new storage, routes, or secret-handling code:

1. The route resolves `source="zendesk"` to the built-in adapter.
2. The route resolves the signing secret through the existing secret resolver seam.
3. `process_inbound` verifies the Zendesk signature, resolves the webhook binding, and delivers through the durable signal bus.

## Business case

Zendesk is a high-value customer-support system of record. Native signed webhook ingress lets Omnigent agents react to customer-support events such as ticket created, ticket updated, escalated, or SLA-risk workflows. This directly advances Omnigent's mission by making autonomous agents easier to embed into third-party business applications where operational work already happens.

Example unlocks:

- Wake an escalation agent when a VIP ticket is updated.
- Trigger an account-research agent when a Zendesk ticket enters a renewal-risk queue.
- Route ticket events to ByteDesk Platform workspaces as durable, auditable autonomous runs.
- Pair Zendesk ingress with future OAuth/action integrations so the same agent can both observe ticket events and act back into Zendesk.

## Future unlocks

- Add a Zendesk connected-app manifest entry once the catalog PR lands in develop.
- Add a preflight/sample payload generator for Zendesk webhook setup.
- Add optional timestamp freshness-window enforcement at the route level when request receipt time is available.
- Add Zendesk action tooling for comment creation, ticket assignment, and status changes after OAuth/secret readiness flows land.

## Verification

TDD cycle performed:

1. Added failing tests for `ZendeskWebhookAdapter` import/behavior and built-in registry resolution.
2. Verified RED with:

   `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_zendesk_adapter_verifies_timestamped_signature_and_event_header tests/ingress/test_ingress.py::test_zendesk_adapter_is_registered_builtin -q`

   Expected failure: `ImportError: cannot import name 'ZendeskWebhookAdapter'`.
3. Implemented the adapter and registry registration.
4. Verified GREEN with the same targeted test command: `2 passed, 1 warning`.
5. Ran the ingress-focused regression slice:

   `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py tests/ingress/test_secret_resolver_seam.py -q`

   Result: `13 passed, 1 warning`.

The full suite was not run because this is a surgical ingress-adapter change and the repository contains a large e2e-heavy test matrix. The targeted slice covers the modified adapter seam, binding resolution flow, signal-bus delivery path, replay handling, expired wait handling, registry behavior, and secret resolver seam.
