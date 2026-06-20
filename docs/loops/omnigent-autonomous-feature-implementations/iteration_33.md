# Iteration 33: Google Workspace push-channel ingress adapter

## Capability shipped

Added a built-in `google-workspace` webhook ingress adapter for Google Workspace push notifications. The adapter authenticates Google Drive, Calendar, Gmail, and Admin SDK watch-channel notifications with `X-Goog-Channel-Token` and routes bindings by `X-Goog-Resource-State` (`sync`, `exists`, `not_exists`, etc.), falling back to `*` when the state header is absent.

This lets Omnigent agents wake from Google Workspace events without forcing those push channels through the default GitHub-style HMAC adapter.

## Prior loop awareness

Before choosing the capability, I inspected open PRs whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop PRs already cover:

- iteration 1: integration capability catalog
- iteration 2: external work item intake
- iteration 3: integration workflow plan compiler
- iteration 4: connected app manifest compiler
- iterations 5-8: Slack, Stripe, GitHub, and JSON payload webhook ingress surfaces
- iterations 9-22: approval, routing, binding, secret readiness, OAuth state, replay, handoff, activation, authorize URL, and deterministic workflow harness work
- iterations 23-32: Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, adapter manifest, GitLab, and declarative HMAC adapter work

To avoid duplicating that work, this iteration adds a non-HMAC Google Workspace push-channel adapter. It complements iteration 32's declarative HMAC direction because Google Workspace uses a shared channel token rather than a body signature.

## Implementation details

Files changed:

- `bytedesk_omnigent/ingress.py`
  - Added `GoogleWorkspaceWebhookAdapter`, a `WebhookSourceAdapter` implementation for Google Workspace push channels.
  - `verify(...)` performs constant-time comparison between `X-Goog-Channel-Token` and the configured ingress secret.
  - `match_key(...)` returns `X-Goog-Resource-State` or `*` for catch-all bindings.
  - Registered the adapter as the built-in `google-workspace` source in the existing webhook adapter registry.
- `tests/ingress/test_ingress.py`
  - Added coverage for token verification, missing-token rejection, resource-state matching, catch-all fallback, and built-in registry resolution.

Operational use:

- Configure `OMNIGENT_INGRESS_SECRET_GOOGLE_WORKSPACE` to match the `X-Goog-Channel-Token` value used when creating Google watch channels.
- Register bindings under source `google-workspace` with match keys such as `sync`, `exists`, or `*`.
- Google POSTs to `/v1/ingress/google-workspace` wake the bound signal when the channel token and resource state match.

## Business case

Google Workspace is a high-priority business integration surface: customers keep work in Drive, Calendar, Gmail, Docs, Sheets, and Admin-managed directories. Agents that can react to Workspace changes can automate document processing, account operations, scheduling workflows, support triage, and ByteDesk Platform follow-ups.

Without this adapter, Google push notifications would be rejected because they do not provide a GitHub-style HMAC header. This capability removes that protocol mismatch and makes Workspace events a first-class wake source for autonomous agents.

## Future unlocks

- Add catalog entries for Drive file-change, Calendar event-change, Gmail mailbox-change, and Admin directory-change watches.
- Compile Google OAuth scopes and watch-channel provisioning into connected-app activation plans.
- Include channel expiration/renewal reminders in the integration workflow harness so agents can keep Google watch channels alive.
- Add a richer match-key strategy if customers need to bind by resource id or channel id in addition to resource state.
- Surface the adapter in an adapter manifest endpoint once the open adapter-manifest PR lands.

## Test plan

Targeted RED/GREEN tests run:

```bash
# RED: failed before production code because GoogleWorkspaceWebhookAdapter did not exist.
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_google_workspace_adapter_verifies_channel_token_and_reads_resource_state -q

# GREEN: new focused adapter + registry tests.
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_google_workspace_adapter_verifies_channel_token_and_reads_resource_state tests/ingress/test_ingress.py::test_google_workspace_source_resolves_bespoke_adapter -q
```

Additional verification before PR:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py
git diff --check
```

The full suite was not run because the code change is isolated to webhook ingress adapter behavior; the targeted ingress suite exercises existing delivery behavior plus the new Google Workspace adapter path.