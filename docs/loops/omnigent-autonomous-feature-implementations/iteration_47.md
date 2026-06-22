# Iteration 47: Built-in Notion webhook ingress adapter

## Capability shipped

This iteration adds a built-in `NotionWebhookAdapter` to Omnigent's signed inbound webhook ingress seam.

The adapter lets a deployment point Notion automation/webhook events at `POST /v1/ingress/notion` and have Omnigent verify provider-native signatures before resolving the event into a durable signal binding. It supports:

- HMAC-SHA256 verification of the raw request body via `X-Notion-Signature`.
- Bare hex and `sha256=<hex>` signature forms, matching the existing constant-time verifier contract.
- Deterministic binding keys from `X-Notion-Event` or `X-Notion-Event-Type` when present.
- Safe fallback to the per-source `"*"` catch-all binding when a workspace/provider shape only exposes event type in payload.
- Built-in registration under source name `notion`, while preserving GitHub as the default adapter for unknown sources.

## Prior loop awareness

Before choosing the feature, I inspected open loop PRs matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Existing open loop work already covers Slack, Stripe, GitHub routing, Microsoft Teams, Shopify, Linear, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Discord, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, and several integration plan/harness compilers. Notion was not represented in the open loop PR set, so this implementation adds a new integration surface instead of duplicating earlier adapter work.

## Implementation details

Changed files:

- `bytedesk_omnigent/ingress.py`
  - Added `NotionWebhookAdapter` implementing the existing `WebhookSourceAdapter` protocol.
  - Registered `notion` in `_build_webhook_adapter_registry()` so route-level `resolve_webhook_adapter(source)` selects it automatically.
  - Kept fallback/default behavior unchanged for all other sources.

- `tests/ingress/test_ingress.py`
  - Added TDD coverage proving Notion signature verification, event-header routing, catch-all fallback, and registry resolution.

The route itself already resolves adapters by source and passes the configured secret plus raw body through `process_inbound`, so no route or database changes were required.

## Business case

Notion is a common operating-system-of-record for startups and teams: specs, project trackers, CRM-lite databases, customer notes, onboarding docs, and product roadmaps often live there. A first-class Notion ingress adapter lets Omnigent agents wake up from trusted Notion events without custom glue code per customer.

That directly advances Omnigent's mission of autonomous agent management and third-party application integration: agents can react to updates in Notion workspaces, coordinate follow-up tasks, and drive ByteDesk Platform workflows from customer-owned knowledge bases.

## Future unlocks

- Add a Notion-specific payload normalizer once the event payload contract is stable in the integration catalog.
- Add a Notion OAuth/connection manifest so customers can configure both inbound webhooks and outbound Notion API tools from one connected-app setup.
- Compile Notion database/page events into task briefs for capability-aware assignment.
- Add ByteDesk Platform UI affordances for binding Notion databases/pages to Omnigent task queues.

## Verification

TDD red step:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_notion_adapter_verifies_signature_and_reads_event_headers tests/ingress/test_ingress.py::test_resolve_webhook_adapter_registers_notion_builtin -q`
- Failed as expected with `ImportError: cannot import name 'NotionWebhookAdapter'` before implementation.

Green / targeted regression:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_notion_adapter_verifies_signature_and_reads_event_headers tests/ingress/test_ingress.py::test_resolve_webhook_adapter_registers_notion_builtin -q`
- Result: `2 passed, 1 warning`.

Ingress regression scope:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py tests/ingress/test_secret_resolver_seam.py -q`
- Result: `13 passed, 1 warning`.

Full suite was not run because this is a surgical ingress-adapter change; the targeted ingress suite exercises the modified seam and adjacent secret resolver behavior.
