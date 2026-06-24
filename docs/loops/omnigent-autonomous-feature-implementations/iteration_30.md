# Omnigent autonomous feature loop iteration 30

## Capability shipped

Added a setup-safe webhook adapter manifest for the inbound ingress surface.

ByteDesk Platform can now call `GET /v1/ingress/adapters` to discover the webhook adapter contracts currently registered in Omnigent without exposing secret values. The response lists each source, accepted signature headers, event/match-key headers, auth scheme, fallback match key, and the conventional environment variable name operators must configure.

This is intentionally narrower than a full integration capability catalog: it focuses on the operational metadata needed to connect external systems to Omnigent's existing signed webhook ingress.

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs targeting `develop`:

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
- #121 iteration 25: Zendesk webhook ingress adapter
- #122 iteration 26: Asana webhook ingress adapter
- #123 iteration 27: HubSpot webhook ingress adapter
- #124 iteration 28: Jira webhook ingress adapter
- #125 iteration 29: Intercom webhook ingress adapter

To avoid duplicating those provider-specific adapters or broader catalog compilers, iteration 30 adds a generic management/readiness surface for whatever adapters are registered on the current runtime.

## Implementation details

Changed files:

- `bytedesk_omnigent/ingress.py`
  - Added `secret_env_name(source)` so the conventional webhook secret env var can be reused by runtime code and metadata.
  - Added `WebhookAdapterDescriptor`, a JSON-safe descriptor for one adapter's external setup contract.
  - Added descriptor storage alongside the existing adapter registry.
  - Extended `register_webhook_adapter(..., descriptor=...)` so built-in or extension-provided adapters can publish setup metadata when they register.
  - Added `describe_webhook_adapters()` to return deterministic, sorted, secret-free adapter metadata.
- `bytedesk_omnigent/routes/ingress.py`
  - Added `GET /ingress/adapters`, mounted by the existing extension under `/v1`, returning `{"adapters": [...]}`.
- `tests/ingress/test_ingress.py`
  - Added coverage for descriptor-backed custom adapter registration.
  - Added HTTP coverage for `GET /v1/ingress/adapters`.
  - Tightened the adapter registry fixture to isolate both registry and descriptor state between tests.

## Business case

Webhook integration is only valuable if customers can configure it correctly. This manifest gives ByteDesk Platform a stable API to render source-specific setup instructions, check whether an adapter is present, and tell an operator exactly which headers and env var are required without leaking credentials.

That reduces time-to-first-integration for customer success teams, makes marketplace/connected-app onboarding less bespoke, and creates a bridge between Omnigent's backend ingress contracts and a future self-serve ByteDesk integration UI.

## Future unlocks

- Merge with a broader `/v1/integration-capabilities` catalog once the open catalog PR lands.
- Have provider-specific adapter PRs register exact descriptors for Slack, Stripe, GitHub, Linear, Jira, HubSpot, Zendesk, Intercom, and other adapters.
- Add readiness status by combining this manifest with secret readiness checks and binding inventory.
- Let ByteDesk Platform generate copy/paste setup instructions and provider-specific webhook configuration forms from the descriptor fields.
- Expose extension-provided descriptors from external packages so third-party developers can add integrations without changing core Omnigent code.

## Verification

Red/green TDD was used:

1. Added failing tests for descriptor metadata and the HTTP manifest endpoint.
2. Confirmed the new test failed before implementation with an import error for the missing descriptor API.
3. Implemented the descriptor model, registry metadata, manifest compiler, and route.
4. Re-ran the targeted ingress tests successfully.

Commands run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q
```

Result: `9 passed, 1 warning in 0.81s`.

Additional checks before PR:

```bash
git diff --check
```
