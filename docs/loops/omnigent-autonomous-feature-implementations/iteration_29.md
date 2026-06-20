# Iteration 29: Intercom webhook ingress adapter

## Capability shipped

Added a built-in Intercom webhook ingress adapter so Omnigent can accept signed Intercom conversation/customer events and route them into durable signal waits through the existing `/v1/ingress/{source}` path.

The adapter maps Intercom's webhook contract into Omnigent's existing source-adapter seam:

- Verifies `X-Hub-Signature` using HMAC-SHA1 over the raw request body.
- Accepts both `sha1=<hex>` and bare hex signature forms.
- Extracts the binding match key from `X-Topic`.
- Falls back to `*` when no topic is present, preserving the existing catch-all binding behavior.
- Registers `intercom` as a first-class built-in webhook source while leaving the GitHub-compatible default adapter unchanged.

## Prior loop awareness

Before selecting this capability, I inspected the currently open loop PRs for `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- Iterations 1-4 established integration capability/catalog/planning surfaces.
- Iterations 5-28 added Slack, Stripe, GitHub, JSON payload, approval, Microsoft Teams, Linear, Shopify, webhook binding, route/replay/handoff/OAuth/activation harness pieces, Discord, Trello, Zendesk, Asana, HubSpot, and Jira integration ingress/planning work.

Intercom was not represented in the open loop PR set, so this iteration adds one non-duplicative customer-support integration that complements Zendesk while expanding coverage to another common support/chat system.

## Implementation details

Changed files:

- `bytedesk_omnigent/ingress.py`
  - Added `IntercomWebhookAdapter` implementing `WebhookSourceAdapter`.
  - Registered `intercom` in the webhook source adapter registry.
- `tests/ingress/test_ingress.py`
  - Added failing-first coverage for Intercom SHA1 signature verification, topic extraction, missing/bad signatures, catch-all topic fallback, and built-in registry resolution.

No secrets or deployment configuration were changed. Operators continue to provide the Intercom webhook secret through the existing `OMNIGENT_INGRESS_SECRET_INTERCOM` environment convention or a custom secret resolver.

## Business case

Intercom is a high-value support and customer-success system. With this adapter, hosted Omnigent agents can be woken by Intercom events such as conversation creation, replies, assignment changes, or customer lifecycle updates. This directly supports ByteDesk/Omnigent's mission to embed autonomous agents into third-party applications and customer-support workflows without bespoke glue code for every deployment.

Example unlocks:

- Auto-triage new Intercom conversations into ByteDesk/Omnigent work queues.
- Wake a parked customer-success agent when a VIP customer replies.
- Coordinate escalation agents when Intercom topics indicate SLA-sensitive events.
- Use one durable signal-bus path for Intercom alongside Jira, Zendesk, HubSpot, Slack, Stripe, and other loop integrations.

## Future unlocks

- Add catalog metadata for Intercom event topics once the integration capability catalog lands in `develop`.
- Add an Intercom OAuth/connect manifest when connected-app setup is available in the base branch.
- Add higher-level routing templates for common topics like `conversation.user.created`, `conversation.user.replied`, and assignment events.
- Surface an operator preflight that validates `OMNIGENT_INGRESS_SECRET_INTERCOM` readiness without exposing the secret.

## Test plan

Targeted tests run from the managed worktree with the canonical virtualenv:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_intercom_adapter_verifies_sha1_signature_and_reads_topic tests/ingress/test_ingress.py::test_resolve_webhook_adapter_has_built_in_intercom_adapter -q
```

Result: `2 passed, 1 warning in 0.14s`.

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q
```

Result: `9 passed, 1 warning in 0.78s`.

The warning is the repo's pre-existing `tests/known_failures.yaml` unmatched-entry warning emitted during collection; it is unrelated to this change.
