# Omnigent autonomous feature loop — iteration 6

## Capability implemented

Implemented a built-in Stripe Events API ingress adapter for ByteDesk Omnigent's signed webhook runtime.

New behavior:

- `POST /v1/ingress/stripe` can now resolve a first-party `StripeWebhookAdapter` without deployment-specific adapter glue.
- Stripe `Stripe-Signature` headers are verified using the standard signed payload shape: `{timestamp}.{raw_body}` with one or more `v1` HMAC-SHA256 signatures.
- Stripe webhook timestamps are enforced with a five-minute replay window before Omnigent attempts to resolve a binding or wake a session.
- Stripe event payloads route to deterministic ingress binding match keys from the signed JSON payload's `type` field, e.g. `invoice.paid`, `checkout.session.completed`, `customer.subscription.updated`, or `charge.dispute.created`.
- The generic webhook adapter seam is now body-aware while preserving header-only adapters, so future integrations whose event names live in the signed payload can use the same route and durable signal bus.

## Prior loop awareness

Before selecting this iteration, I inspected open loop PRs in `ByteDeskAI/bytedesk-omnigent`:

- PR #96 / iteration 1: integration capability catalog (`/v1/integration-capabilities`).
- PR #98 / iteration 2: external work-item intake.
- PR #99 / iteration 3: deterministic integration workflow plan compiler.
- PR #100 / iteration 4: connected-app manifest compiler.
- PR #101 / iteration 5: Slack webhook ingress adapter.

This iteration avoids duplicating those catalog, intake, workflow-plan, connected-app manifest, and Slack surfaces. It adds one concrete runtime adapter for a popular revenue/service integration from the catalog direction: Stripe-originated business events can now safely wake Omnigent sessions via the existing durable signal bus.

## Business case

Stripe is the system of record for revenue events. Giving Omnigent native, signature-verified Stripe ingress lets ByteDesk Platform and third-party embedded deployments react to customer lifecycle and billing signals without custom webhook glue. Examples include:

- Start onboarding or success workflows after `checkout.session.completed`.
- Wake finance/revenue agents after `invoice.paid` or `invoice.payment_failed`.
- Escalate support or retention workflows on subscription changes.
- Trigger dispute-response agents on `charge.dispute.created`.
- Drive marketplace/agent-credit fulfillment from verified payment events.

This directly supports Omnigent's mission of letting autonomous agents coordinate with third-party applications and ByteDesk Platform systems where real work and revenue events originate.

## Implementation notes

- Updated `bytedesk_omnigent.ingress.WebhookSourceAdapter` so `match_key` can receive `raw_body` and the parsed JSON `payload`.
- Added `StripeWebhookAdapter` and registered it as the built-in adapter for source `stripe`.
- Added Stripe signature parsing, timestamp tolerance checks, and payload-type match-key routing.
- Kept existing GitHub/default and custom header-only adapter behavior compatible through an adapter match-key helper.
- Added regression tests for Stripe signature verification, replay-window rejection, built-in adapter resolution, and end-to-end delivery through `process_inbound` to a durable signal-bus wait.

## Future unlocks

- Feed iteration 4 connected-app manifests with Stripe-specific webhook event defaults and required secret names.
- Use iteration 3 workflow plans to compile Stripe event types into deterministic approval/writeback flows.
- Attach Stripe events to iteration 2 work-item intake when a billing event should become a durable task rather than immediately waking an existing wait.
- Add provider-specific adapters for HubSpot, Salesforce, Zendesk, Intercom, Shopify, GitHub Apps, Linear, Jira, and Microsoft Teams using the same body-aware adapter seam.
- Add tenant/workspace installation records so ByteDesk Platform can manage multiple Stripe accounts and rotate secrets without process-level environment changes.

## Verification

Targeted verification was run because this feature only touches the signed ingress adapter seam and its tests:

- `PYTHONPATH="$PWD" uv run --extra dev python -m pytest tests/ingress/test_ingress.py -q`
- `PYTHONPATH="$PWD" uv run --extra dev python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py`
- `git diff --check`
