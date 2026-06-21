# Omnigent autonomous feature loop — iteration 12

## Capability shipped

Built-in Shopify webhook ingress support for Omnigent's signed inbound event pipeline.

Omnigent already exposes a durable `/v1/ingress/{source}` seam that verifies a source adapter, resolves a `(source, match_key)` webhook binding, and wakes a parked signal wait. Iteration 12 adds a first-class `shopify` source adapter so commerce and marketplace events can wake autonomous agents without custom relay code.

## Prior loop awareness

Before selecting this feature, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- PR #96 / iteration 1: integration capability catalog (`/v1/integration-capabilities`).
- PR #98 / iteration 2: external work-item intake.
- PR #99 / iteration 3: deterministic integration workflow plan compiler.
- PR #100 / iteration 4: connected-app manifest compiler.
- PR #101 / iteration 5: Slack webhook ingress adapter.
- PR #102 / iteration 6: Stripe webhook ingress adapter.
- PR #103 / iteration 7: GitHub webhook event routing.
- PR #104 / iteration 8: JSON payload webhook adapter.
- PR #105 / iteration 9: integration approval plan compiler.
- PR #106 / iteration 10: Microsoft Teams webhook ingress adapter.
- PR #107 / iteration 11: Linear webhook ingress adapter.

This PR avoids duplicating those open branches by adding a distinct commerce connector: Shopify-originated store events.

## Implementation details

- Added `ShopifyWebhookAdapter` in `bytedesk_omnigent/ingress.py`.
- Registered it in the webhook adapter registry under source `shopify`.
- Verifies Shopify's standard `X-Shopify-Hmac-Sha256` header: base64-encoded HMAC-SHA256 over the raw request body using the existing source secret.
- Routes events by Shopify's `X-Shopify-Topic` header, for example `orders/create`, `orders/paid`, `customers/create`, or `app/uninstalled`.
- Keeps existing secret resolution: deployments configure `OMNIGENT_INGRESS_SECRET_SHOPIFY` or install the existing secret resolver strategy.
- Preserves fail-closed ingress semantics: bad signatures return 401, missing bindings return 404, and only a successful durable signal delivery returns 202.

## Business case

Shopify is a core commerce system of record. Native Shopify ingress lets ByteDesk Platform and Omnigent deployments react to revenue, fulfillment, customer, and app-lifecycle signals directly from merchant stores.

High-value autonomous workflows include:

- Wake fulfillment or support agents when `orders/create` or `orders/paid` arrives.
- Trigger customer-success agents on high-value orders or customer lifecycle events.
- Start refund/dispute review workflows from commerce events while preserving approval gates.
- Coordinate marketplace agent-credit fulfillment from verified store purchases.
- Alert operators or automatically pause store-specific agents on `app/uninstalled`.

This expands Omnigent's third-party integration surface beyond collaboration, engineering, and billing systems into commerce operations where autonomous agents can create measurable revenue and customer-service value.

## Future unlocks

- Add Shopify connected-app manifest templates once the iteration 4 manifest compiler lands.
- Feed Shopify order events into the iteration 2 external work-item intake path when events should create durable Tasks instead of waking an existing wait.
- Add Shopify Admin API writeback tools for order notes, tags, fulfillment updates, and customer timeline comments behind approval policies.
- Surface Shopify adapter setup instructions through the iteration 1 integration capability catalog.
- Compose deterministic Archon-style workflows such as `orders/create -> classify risk -> spawn support/fulfillment specialist -> approve writeback -> tag order`.

## Verification

Targeted tests added in `tests/ingress/test_ingress.py` cover:

- Shopify base64 HMAC signature acceptance/rejection.
- Topic-derived match keys from `X-Shopify-Topic`.
- Built-in adapter registry resolution for `shopify`.
- End-to-end `process_inbound` delivery for a `shopify` binding.

Run scope for this iteration:

```bash
PYTHONPATH="$PWD" uv run --extra dev python -m pytest tests/ingress/test_ingress.py -q
PYTHONPATH="$PWD" uv run --extra dev python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py
git diff --check
```

Targeted scope only: this iteration changes the signed ingress adapter seam, focused ingress tests, and loop documentation, so a full repository suite was not run.
