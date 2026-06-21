# Iteration 39: Monday.com webhook ingress adapter

## Capability shipped

This iteration adds a built-in `MondayWebhookAdapter` to Omnigent's inbound webhook ingress layer.

Teams can now route Monday.com board/item automation events into Omnigent via the existing `POST /v1/ingress/{source}` surface by using either `source=monday` or `source=monday.com`. The adapter verifies Monday-specific webhook signatures, extracts a Monday-specific event routing key, and then reuses the existing durable `(source, match_key) -> signal_id` binding flow to wake parked agent sessions.

## Prior loop awareness

Before selecting this work, I inspected open loop PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers the integration catalog and many ingress surfaces:

- Iterations 1-4: catalog/manifest/workflow planning foundations.
- Iterations 5-8: Slack, Stripe, GitHub event routing, JSON payload adapter.
- Iterations 9-22: approval, Teams, Linear, Shopify, binding APIs, route compiler, secret readiness, OAuth state/URL, preflight/replay/handoff/workflow harness compilers.
- Iterations 23-38: Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, adapter manifest, GitLab, declarative HMAC, Google Workspace, rollback/task brief/probe compilers, Airtable, CloudEvents.

Monday.com was not represented in the open loop PR list, so this iteration adds one non-duplicative third-party integration capability rather than overlapping the existing Slack/Airtable/CloudEvents/etc. work.

## Implementation details

Changed files:

- `bytedesk_omnigent/ingress.py`
  - Added `MondayWebhookAdapter`.
  - Verifies `X-Monday-Signature` or normalized `X-Omnigent-Signature` using the existing constant-time HMAC-SHA256 verifier.
  - Accepts both bare hex and `sha256=<hex>` signatures, matching existing Omnigent ingress normalization behavior.
  - Reads the route match key from `X-Monday-Event` and falls back to `*` so catch-all Monday bindings still work.
  - Registers built-in aliases `monday` and `monday.com` in the webhook adapter registry while leaving unknown sources on the existing GitHub-compatible default.

- `tests/ingress/test_ingress.py`
  - Added TDD coverage for signature verification, case-insensitive headers, bad/missing signatures, match-key extraction, registry aliases, and default fallback preservation.

## Business case

Monday.com is a common operations and project-management hub for non-technical teams. Adding a built-in adapter lets Omnigent agents react to Monday board/item activity without each deployment having to write custom glue.

Concrete buyer-facing value:

- Turn Monday item updates into autonomous follow-up tasks.
- Wake delivery, support, or revenue agents when a board status changes.
- Connect customer operations workflows to ByteDesk/Omnigent without bespoke integration code.
- Expand Omnigent's marketplace story from developer-centric tools into business-operations tooling.

## Future unlocks

- Add a Monday capability entry to `/v1/integration-capabilities` once the catalog PR lands.
- Add payload-aware routing so Monday event type can be derived directly from body fields when no `X-Monday-Event` normalization header is present.
- Add an OAuth/connected-app manifest for Monday API scopes and installation URLs.
- Add a deterministic integration probe that signs a sample Monday event and confirms the target binding wakes the intended signal.
- Add ByteDesk Platform UI affordances for creating Monday board/item bindings from the integration marketplace.

## Test plan

Targeted tests run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_monday_adapter_verifies_signature_and_reads_monday_event tests/ingress/test_ingress.py::test_resolve_webhook_adapter_registers_monday_builtin -q
```

Result: `2 passed, 1 warning`.

Additional verification to run before PR completion:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py
git diff --check
```
