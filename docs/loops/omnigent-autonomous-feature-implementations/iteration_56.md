# Iteration 56: deterministic integration backfill plans

## Capability shipped

This iteration adds a deterministic third-party integration backfill plan compiler in `bytedesk_omnigent.integration_backfill`.

The compiler turns a historical-sync request for systems such as Slack, Notion, GitHub, Linear, Google Workspace, or similar connected apps into a bounded, read-only, resumable plan. Each plan includes:

- normalized source/resource/workspace identifiers;
- a durable checkpoint key;
- an idempotency scope for imported records;
- a webhook/signal match key for downstream completion;
- explicit fetch/commit steps for each bounded page;
- safety notes that keep historical imports read-only and checkpointed.

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs for `feature/loop/omnigent-autonomous-feature-implementations/iteration_*` and avoided duplicating the already-open work:

- integration capability catalog and workflow plan work (#96, #99);
- connected app manifests (#100);
- webhook ingress adapters for Slack, Stripe, GitHub, Linear, Teams, Shopify, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, Sentry (#101-#149 range);
- OAuth/state/scope/refresh/activation/secret/readiness/retry/idempotency/contract compiler work (#105, #111, #112, #116, #117, #140, #147, #150, #151);
- workflow harness, replay, rollback, event route, task brief, dead-letter, rate-limit, event envelope, and blueprint preview compilers (#110, #114, #118, #130, #131, #138, #142, #148, #146).

Historical backfill/import planning was not represented in the open loop PR list, and it complements future webhook ingestion: agents need context that existed before a webhook subscription was created.

## Implementation details

Added:

- `bytedesk_omnigent/integration_backfill.py`
  - `IntegrationBackfillRequest`
  - `BackfillStep`
  - `IntegrationBackfillPlan`
  - `compile_backfill_plan(...)`

The compiler enforces bounded work before execution:

- `max_pages` must be 1-100;
- `page_size` must be 1-1000;
- source, resource, workspace, and start cursor are required;
- required scopes must be read-only, rejecting write/admin/delete/send/post style scopes.

A compiled plan uses deterministic keys such as:

- `integration-backfill:<workspace>:<source>:<resource>` for checkpoints;
- `integration-backfill/<source>/<resource>` for idempotency;
- `<resource>.backfill.completed` for completion events.

## Business case

Webhook-first integrations only capture future events. Customers adopting Omnigent will already have valuable context in Slack channels, Notion workspaces, GitHub issues, Linear tickets, Google Drive files, CRM objects, and support queues.

This feature gives Omnigent a safe primitive for onboarding that historical context into autonomous workflows without letting agents improvise dangerous or unbounded imports. That unlocks:

- faster customer onboarding because agents can start from existing records;
- safer enterprise adoption through read-only scopes and page limits;
- resumable imports that survive restarts and avoid duplicate task creation;
- a reusable bridge from third-party history into Omnigent tasks, signals, and agent memory.

## Future unlocks

- Expose a `POST /v1/integration-backfills/plan` preview endpoint for the ByteDesk Platform UI.
- Add connector-specific resource presets for Slack, Notion, GitHub, Linear, Google Workspace, HubSpot, Salesforce, Zendesk, Intercom, Stripe, Shopify, Teams, Discord, Asana, Monday, Airtable, and Jira.
- Persist checkpoints in the existing conversation/task store so runners can resume plans across replicas.
- Emit `completion_match_key` through the existing signal bus after the final committed page.
- Combine with idempotency-key compiler work once the open iteration PRs land.

## Test plan

Targeted tests were run because the change is a surgical pure-Python module with no server wiring or migrations:

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_backfill_plan.py -q`
  - failed as expected before implementation with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_backfill'`.
- GREEN: same command after implementation
  - `2 passed, 1 warning in 0.12s`.

Additional verification before PR:

- `ruff check bytedesk_omnigent/integration_backfill.py tests/test_integration_backfill_plan.py`
- `git diff --check`
