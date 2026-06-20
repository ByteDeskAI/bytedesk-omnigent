# Iteration 24 — Trello signed webhook ingress adapter

## Capability shipped

This iteration adds a built-in `TrelloWebhookAdapter` for Omnigent's signed inbound webhook ingress. Trello webhooks send `X-Trello-Webhook` as a base64-encoded HMAC-SHA1 digest of `raw_body + callback_url` using the Trello application secret. Omnigent can now verify that contract through the existing `WebhookSourceAdapter` registry without deployment-specific adapter code.

Operators can enable Trello ingress by configuring the existing source secret resolver value for `trello` (for the env resolver: `OMNIGENT_INGRESS_SECRET_TRELLO`) to the Trello app secret, then binding either:

- `source="trello", match_key="cardMoved"` when the ingress edge supplies `X-Trello-Action-Type: cardMoved`, or
- `source="trello", match_key="*"` for a catch-all Trello board/list/card binding.

Because Trello signs the callback URL that was registered for the webhook, the adapter expects that URL in `X-Trello-Callback-Url`. This keeps verification deterministic across local, staging, and production ByteDesk deployments without hardcoding a public base URL in Omnigent.

## Prior loop awareness

Before choosing this feature, I inspected the open autonomous loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*` in `ByteDeskAI/bytedesk-omnigent`. The currently open loop work covers:

- iteration 1: integration capability catalog
- iteration 2: external work item intake
- iteration 3: integration workflow plan compiler
- iteration 4: connected app manifest compiler
- iterations 5, 6, 7, 8, 10, 11, 12, 23: Slack, Stripe, GitHub, JSON payload, Microsoft Teams, Linear, Shopify, and Discord webhook ingress adapters/routes
- iterations 9, 14, 18, 19, 20, 21, 22: approval/event-route/replay/handoff/activation/OAuth/harness compiler surfaces
- iterations 13, 15, 16, 17: webhook binding management, secret readiness, OAuth state tokens, and ingress preflight preview

Trello was not covered by those open PRs. This change avoids duplicating the workflow harness, OAuth, replay, handoff, activation, and already-added webhook adapter work; it extends the existing adapter seam with one additional high-value integration source.

## Implementation details

Changed code:

- `bytedesk_omnigent/ingress.py`
  - Adds `TrelloWebhookAdapter` implementing the existing `WebhookSourceAdapter` protocol.
  - Verifies Trello's base64 HMAC-SHA1 signature over `raw_body + callback_url` using only the standard library.
  - Reads `X-Trello-Action-Type` as the binding match key, falling back to `"*"` for source-level catch-all bindings.
  - Registers `trello` in the default webhook adapter registry.
- `tests/ingress/test_ingress.py`
  - Adds coverage for valid Trello HMAC-SHA1 verification.
  - Adds coverage for invalid signatures and missing callback URL rejection.
  - Adds coverage that `resolve_webhook_adapter("trello")` works without custom registration.

No secrets were read or modified.

## Business case

Trello remains a popular lightweight project-management and operations board for small businesses, agencies, and internal teams. Native Trello verification lets ByteDesk/Omnigent agents safely react to card movement, checklist updates, comments, and board workflow events without trusting unsigned webhooks.

Practical agent workflows unlocked:

- Turn a Trello card moved to `Ready for AI` into an Omnigent task or agent session.
- Let an autonomous project-manager agent update ByteDesk/Omnigent work based on Trello board state.
- Coordinate human approvals through Trello columns while Omnigent handles execution in ByteDesk Platform.
- Support agencies and SMBs that use Trello instead of Jira/Linear/Monday/Asana.

The implementation is surgical: it reuses the current ingress route, binding store, signal bus, secret resolver, and adapter registry.

## Future unlocks

- Add a small Trello edge example that maps `action.type` from the JSON body into `X-Trello-Action-Type` and sets `X-Trello-Callback-Url` to the registered callback URL.
- Add a Trello work-item normalizer that maps cards, lists, labels, members, and comments into ByteDesk task intake records.
- Add catalog metadata for Trello once the integration capability catalog lands on `develop`.
- Pair Trello webhooks with OAuth/token vaulting so Omnigent can both receive board events and write back card comments/status changes.

## Verification

TDD cycle run from the iteration 24 worktree:

1. Added Trello adapter tests first.
2. Ran the targeted tests and observed the expected RED import failure because `TrelloWebhookAdapter` did not exist.
3. Implemented the adapter and registry registration.
4. Re-ran the targeted tests successfully.

Targeted RED command:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest \
  tests/ingress/test_ingress.py::test_trello_adapter_verifies_hmac_sha1_and_reads_action_type \
  tests/ingress/test_ingress.py::test_trello_adapter_is_registered_by_default -q
```

Expected RED result observed: import error for missing `TrelloWebhookAdapter`.

Targeted GREEN result observed after implementation:

- `2 passed, 1 warning in 0.12s`

Additional verification run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py
git diff --check
```

Results:

- `9 passed, 1 warning in 0.70s`
- `ruff`: `All checks passed!`
- `git diff --check`: passed with no whitespace errors

The warning is the repo's existing `tests/known_failures.yaml` unmatched-entry warning surfaced during collection, not introduced by this change.
