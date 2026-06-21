# Iteration 21 — OAuth authorization URL compiler

## Capability shipped

Added a deterministic third-party OAuth authorization URL compiler and mounted API route for ByteDesk integration installs:

- `bytedesk_omnigent.integration_authorization.compile_oauth_authorization_url(...)`
- `POST /v1/integration-authorizations/authorize-url`

The compiler turns a provider slug, client id, redirect URI, signed state token, scopes, and optional extra params into the exact admin install URL for supported SaaS providers. It stores no secrets, performs no network I/O, and leaves OAuth state-token creation plus callback code exchange to existing/future platform seams.

Initial provider specs cover high-value marketplace/integration targets:

- Slack
- GitHub
- Linear
- Notion
- Google Workspace

## Prior loop awareness

Before choosing this work, I inspected the currently open autonomous loop PRs whose heads match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #96 iteration 1: integration capability catalog
- #98 iteration 2: external work item intake
- #99 iteration 3: integration workflow plan compiler
- #100 iteration 4: connected app manifest compiler
- #101 iteration 5: Slack webhook ingress adapter
- #102 iteration 6: Stripe webhook ingress adapter
- #103 iteration 7: route GitHub webhook events
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

This iteration intentionally avoids duplicating webhook ingress adapters, state-token minting, readiness plans, activation gates, or handoff/replay compilers. It builds the next missing OAuth install step: given an already-issued signed state token and app config, produce the deterministic provider authorization URL that ByteDesk Platform can show or redirect an admin to.

## Implementation details

### Pure compiler

`bytedesk_omnigent/integration_authorization.py` adds:

- `OAuthProviderSpec` static provider metadata
- `OAuthAuthorizationUrl` result object
- `OAUTH_PROVIDER_SPECS` registry for Slack, GitHub, Linear, Notion, and Google Workspace
- `compile_oauth_authorization_url(...)`
- validation errors for unknown providers and missing required inputs

Compiler behavior:

- requires `provider`, `client_id`, `redirect_uri`, and `state`
- uses provider default scopes when custom scopes are omitted
- deduplicates scopes while preserving order
- uses provider-specific scope separators (`Slack` and `Linear` comma-separated; GitHub/Google space-separated)
- merges provider default extra params and caller extra params deterministically
- returns the encoded URL and normalized scopes

### API route

`bytedesk_omnigent/routes/integration_authorization.py` adds:

- `POST /integration-authorizations/authorize-url`
- the same auth behavior as other ByteDesk control-plane routes: authenticated in multi-user mode, open in single-user mode
- safe 400 responses for invalid requests or unknown providers

`bytedesk_omnigent/extension.py` mounts the router through the existing ByteDesk extension seam so the route appears under `/v1` in the Omnigent server.

## Business case

OAuth install links are the front door for turning Omnigent into an integration marketplace rather than a one-off webhook receiver. This capability gives ByteDesk Platform a deterministic, testable way to launch admin install flows for popular services without embedding provider-specific URL logic in the UI or leaking secrets into client code.

Practical impact:

- Faster connected-app onboarding for business users
- Fewer hand-coded integration setup paths in ByteDesk Platform
- Cleaner separation between install URL generation, OAuth state issuance, and callback/token exchange
- A stable surface for agents to request third-party connection setup as part of autonomous task execution

## Future unlocks

- Connect this compiler to the open OAuth state-token capability so the route can mint or validate state handles directly.
- Add provider specs for Jira, Trello, HubSpot, Salesforce, Zendesk, Intercom, Stripe Connect, Shopify, Microsoft Teams, Discord, Asana, Monday, and Airtable.
- Add callback code-exchange adapters that transform provider auth codes into vault-backed secret references.
- Feed successful installs into connected app manifests and activation gates.
- Expose safe install-link previews in ByteDesk Platform so admins can verify scopes before redirect.

## Verification

Targeted TDD cycle:

1. Added `tests/test_integration_authorization.py` first.
2. Ran the test file and observed the expected RED failure: missing `bytedesk_omnigent.integration_authorization` and route modules.
3. Implemented the compiler, route, and extension mount.
4. Re-ran the targeted tests successfully.

Commands run:

```bash
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_authorization.py -q
```

Result:

- `5 passed, 1 warning in 0.15s`

The warning is the repo's existing `tests/known_failures.yaml` unmatched-entry warning surfaced during collection, not introduced by this change.
