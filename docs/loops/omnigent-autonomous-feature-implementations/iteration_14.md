# Omnigent autonomous feature loop — iteration 14

## Capability shipped

Deterministic integration event route compiler for connected-app onboarding.

New surface:

- `POST /v1/integration-event-routes/compile`
- Pure compiler module: `bytedesk_omnigent.integration_event_routes`
- Route module: `bytedesk_omnigent.routes.integration_event_routes`

The compiler turns a third-party event descriptor into a previewable Omnigent routing plan containing:

- Canonical provider / ingress source slug.
- Binding match key for webhook or event subscriptions.
- Required specialist capability used by agent routing.
- External task kind to create or resume.
- Stable idempotency key to avoid duplicate autonomous work on retries.
- Approval and writeback policy.
- Deterministic harness steps inspired by Archon-style workflows: verify connected app, normalize event, resolve specialist, create/resume task, approval gate, and provider writeback.

## Prior loop awareness

Before selecting this feature, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #96 / iteration 1: integration capability catalog.
- #98 / iteration 2: external work-item intake.
- #99 / iteration 3: integration workflow plan compiler.
- #100 / iteration 4: connected app manifest compiler.
- #101 / iteration 5: Slack webhook ingress adapter.
- #102 / iteration 6: Stripe webhook ingress adapter.
- #103 / iteration 7: GitHub webhook event routing.
- #104 / iteration 8: JSON payload webhook adapter.
- #105 / iteration 9: integration approval plan compiler.
- #106 / iteration 10: Microsoft Teams webhook ingress adapter.
- #107 / iteration 11: Linear webhook ingress adapter.
- #108 / iteration 12: Shopify webhook ingress adapter.
- #109 / iteration 13: webhook binding management API.

This iteration avoids duplicating those open branches. Instead of adding another provider-specific adapter or another manifest/approval surface, it fills the planning gap between an installed connected app event and autonomous Omnigent execution: what capability should handle the event, what task kind should be created, what idempotency key should be used, and whether writeback requires approval.

## Business case

ByteDesk Platform will need to install many connected apps and route their events into Omnigent agents without bespoke glue per provider. A deterministic route compiler lets the platform, an integration wizard, or a future marketplace connector preview and store the same execution contract every time. That reduces connector implementation time, prevents duplicate tasks during webhook retries, and makes it easier to explain why a provider event woke a specific autonomous specialist.

Supported provider profiles include developer work systems, project-management tools, knowledge/workspace apps, chatops, support desks, CRM/revenue systems, commerce, and data tools: GitHub, GitLab, Linear, Jira, Trello, Asana, Monday, Notion, Google Workspace, Slack, Microsoft Teams, Discord, Zendesk, Intercom, HubSpot, Salesforce, Stripe, Shopify, and Airtable. Unknown providers degrade deterministically to `integration.generic_event` and an `external.<provider>.event` task kind.

## Future unlocks

- Feed compiler output into the open external work-item intake and webhook binding-management PRs once they land.
- Persist route plans as connected-app installation records in ByteDesk Platform.
- Link route plans to the integration capability catalog so Office can recommend next best connectors.
- Attach route-plan IDs to webhook bindings for observability and replay.
- Materialize the plan steps into durable tool-step/workflow harness records for deterministic execution and recovery.
- Extend provider profiles with tenant-specific policy overrides, SLA/priority classes, and writeback destinations.

## Test plan

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_event_routes.py -q` failed before implementation with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_event_routes'`.
- GREEN: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_event_routes.py -q` passed: `3 passed, 1 warning`.

Full-suite pytest was not run because this is a surgical compiler/route addition and the targeted test covers the pure compiler, fallback behavior, and HTTP route wiring.
