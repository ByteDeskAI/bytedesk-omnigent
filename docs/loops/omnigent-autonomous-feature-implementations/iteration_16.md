# Omnigent autonomous feature loop — iteration 16

## Capability shipped

Iteration 16 adds deterministic connected-app OAuth state token issuance and verification for ByteDesk Platform integration handoffs.

New surface:

- `POST /v1/integration-oauth-states/issue`
- `POST /v1/integration-oauth-states/verify`
- Pure compiler/verifier module: `bytedesk_omnigent.integration_oauth_states`
- Route module: `bytedesk_omnigent.routes.integration_oauth_states`

The module issues compact `omni-oauth-v1.<payload>.<signature>` state tokens that bind:

- Provider slug, normalized from names like `Google Workspace` or `HubSpot`.
- ByteDesk workspace id.
- Redirect/callback URI.
- Requested OAuth scopes, de-duplicated and sorted deterministically.
- Connected-app install id.
- CSRF nonce.
- Issued/expiry timestamps.

Tokens are signed with `OMNIGENT_OAUTH_STATE_SECRET` via HMAC-SHA256. The signing secret is never embedded in the token or response. Verification fail-closes with structured reasons for malformed, wrong-version, bad-signature, expired, provider-mismatch, and workspace-mismatch callbacks.

## Prior loop awareness

Before selecting this capability, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #96 / iteration 1: integration capability catalog.
- #98 / iteration 2: external work-item intake.
- #99 / iteration 3: integration workflow plan compiler.
- #100 / iteration 4: connected-app installation manifest compiler.
- #101 / iteration 5: Slack webhook ingress adapter.
- #102 / iteration 6: Stripe webhook ingress adapter.
- #103 / iteration 7: GitHub webhook event routing.
- #104 / iteration 8: JSON payload webhook adapter.
- #105 / iteration 9: integration approval plan compiler.
- #106 / iteration 10: Microsoft Teams webhook ingress adapter.
- #107 / iteration 11: Linear webhook ingress adapter.
- #108 / iteration 12: Shopify webhook ingress adapter.
- #109 / iteration 13: webhook binding management API.
- #110 / iteration 14: integration event route compiler.
- #111 / iteration 15: integration secret readiness plans.

This iteration avoids duplicating those branches. Instead of adding another webhook adapter, approval compiler, event route compiler, connected-app manifest, or secret readiness plan, it fills the OAuth install callback integrity gap: safely carrying install context from ByteDesk Platform through third-party authorization screens and back into Omnigent/Platform without server-side one-off glue per provider.

The canonical checkout had unrelated uncommitted work, so the managed worktree command initially refused to create the branch. I reran the same managed operator with `--allow-dirty`, left canonical WIP untouched, and verified the resulting branch was exactly `feature/loop/omnigent-autonomous-feature-implementations/iteration_16`.

## Implementation details

- Added `OAuthStateClaims`, `IssuedOAuthState`, and `OAuthStateVerification` dataclasses with `to_dict()` serializers for route responses.
- Added `issue_oauth_state()` to normalize provider names, validate required install fields, enforce a 60..3600 second TTL, and sign deterministic JSON claims.
- Added `verify_oauth_state()` to verify token version, HMAC signature, expiry, expected provider, and expected workspace.
- Added route helpers under `/v1/integration-oauth-states/issue` and `/v1/integration-oauth-states/verify`, using `OMNIGENT_OAUTH_STATE_SECRET` as the signing key.
- Registered the router through `BytedeskExtension.routers()` so the ByteDesk extension exposes it alongside the existing governance, ingress, goals, and task APIs.
- Kept the implementation dependency-free: only stdlib HMAC, SHA-256, base64url, JSON, and dataclasses.

## Business case

Popular OAuth integrations such as Google Workspace, HubSpot, Salesforce, Zendesk, Intercom, Notion, Slack, Linear, GitHub, Trello, Jira, Microsoft Teams, Discord, Asana, Monday, Airtable, Stripe, and Shopify all require a safe install handoff from ByteDesk Platform to an external authorization screen and back.

Without a deterministic signed state contract, Platform either needs bespoke per-provider callback state storage or risks callback confusion: an OAuth code could be exchanged for the wrong provider, wrong workspace, wrong redirect URI, or stale install attempt. This capability makes connected-app onboarding safer and more productizable:

1. Platform asks Omnigent to issue a short-lived state token before redirecting the user to a provider.
2. The provider redirects back with `code` and `state`.
3. Platform/Omnigent verifies the token before exchanging the code or enabling event routing.
4. Downstream manifest, secret readiness, approval, webhook binding, and event route plans can trust the install context.

That reduces connector implementation effort, prevents cross-workspace install mistakes, and gives future marketplace connector authors a stable OAuth install primitive.

## Future unlocks

- Bind state tokens to connected-app manifest ids once the open manifest branch lands.
- Persist verified install attempts for audit trails and replay protection.
- Add provider-specific callback URI allowlists from the integration capability catalog.
- Feed verified claims into secret readiness plans so OAuth client/scopes can be checked before code exchange.
- Attach verified install ids to webhook bindings and integration event route plans.
- Add optional PKCE challenge metadata for providers that support public-client installs.

## Test plan

Targeted tests and lint were run because this iteration touched one pure module, one thin route, extension route registration, docs, and focused unit/API coverage rather than the full runtime.

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_oauth_states.py -q`
  - Expected failure observed: `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_oauth_states'`.
- GREEN: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_oauth_states.py -q`
  - Result: `5 passed, 1 warning in 0.13s`.
- Lint: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_oauth_states.py bytedesk_omnigent/routes/integration_oauth_states.py bytedesk_omnigent/extension.py tests/bytedesk_omnigent/test_integration_oauth_states.py`
  - Result: `All checks passed!`.
- Whitespace: `git diff --check`
