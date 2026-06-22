# Omnigent autonomous feature loop — iteration 13

## Capability shipped

Webhook binding management API for connected-app ingress.

Prior loop PRs established Omnigent's integration catalog, deterministic workflow planning, approval-plan compiler, external work-item intake, and a growing set of signed webhook adapters for providers such as Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, and generic JSON payloads. This iteration avoids duplicating those open loop branches by filling a management gap in the existing ingress substrate: ByteDesk Platform and operator tooling can now create and inspect webhook-to-signal bindings through Omnigent's API instead of reaching into the database or requiring bespoke deployment scripts.

## Implementation details

- Adds `IngressBindingStore.list_bindings(source=None, enabled=None)` with deterministic ordering by `source` and `match_key`.
- Adds `GET /v1/ingress-bindings` to list current bindings, optionally filtered by `source` and `enabled`.
- Adds `POST /v1/ingress-bindings` to idempotently create/update a `(source, match_key) -> signal_id` binding.
- Keeps `POST /v1/ingress/{source}` unauthenticated because webhook delivery is authenticated by the provider signature, while management endpoints call `require_user` when Omnigent is running with an auth provider.
- Wires the existing ByteDesk extension to pass the server auth provider into the ingress router.
- Adds targeted tests covering deterministic store listing, route create/list/update behavior, and validation for missing required fields.

## Business case

A connected-app marketplace needs more than provider-specific signature verification. The platform must install a workflow, park an agent/session on a durable signal, and register which external event wakes it. Exposing binding management as an API lets ByteDesk Platform, a future integration wizard, or an app-install manifest compiler provision webhook routes without privileged database access.

This shortens integration setup from manual SQL/configuration to a deterministic API call:

1. Create or resume an autonomous workflow wait.
2. Register `source + event match_key -> signal_id` through `/v1/ingress-bindings`.
3. Configure the provider webhook URL and secret.
4. Let the signed `/v1/ingress/{source}` path wake the waiting agent when the external event arrives.

## Future unlocks

- ByteDesk Platform UI can show installed webhook bindings and their target signals per workspace/app.
- Connected-app manifest installation can call the binding API as its final provisioning step.
- Provider adapters can expose recommended `match_key` values and have the installer pre-create the right bindings.
- A later hardening pass can add disable/delete endpoints plus audit events for compliance-sensitive apps.
- The integration catalog can link capability blueprints directly to binding-management recipes.

## Verification

Targeted lint/test commands:

```bash
PYENV_VERSION=system uv run --extra dev ruff check bytedesk_omnigent/ingress.py bytedesk_omnigent/routes/ingress.py bytedesk_omnigent/extension.py tests/ingress/test_ingress_bindings_api.py
PYENV_VERSION=system uv run --extra dev python -m pytest tests/ingress/test_ingress.py tests/ingress/test_ingress_bindings_api.py -q
```
