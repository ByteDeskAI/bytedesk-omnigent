# Omnigent autonomous feature loop iteration 3

## Capability: deterministic integration workflow plan compiler

Iteration 3 adds a small, side-effect-free compiler for connected-app workflow plans:

- `bytedesk_omnigent/integration_workflow_plans.py`
- `POST /v1/integration-workflow-plans/compile`
- `tests/bytedesk_omnigent/test_integration_workflow_plans.py`

The compiler accepts a provider, goal, source object reference, requester, optional context refs, optional caller idempotency key, and approval/writeback overrides. It returns a deterministic plan that a workflow harness, ByteDesk Platform, Office, webhook adapter, or OAuth connector can execute later:

1. normalize the provider event into a provider-neutral envelope
2. fetch referenced source-system context deterministically
3. resolve the right Omnigent agent by required capability
4. optionally request app-scoped approval for risky systems of record
5. run exactly one capability-scoped agent turn
6. optionally write the result back to the source app
7. record outcome metadata for future routing/governance

This is intentionally not a full connector and does not duplicate the open loop PRs:

- PR #96 / iteration 1 adds a ranked integration capability catalog.
- PR #98 / iteration 2 adds external work-item intake.
- This PR adds the deterministic harness plan shape those catalog entries and intake records can use before execution.

## Why this matters

Omnigent's mission is to be agent middleware for autonomous agent creation, management, coordination, and integration into third-party applications and ByteDesk Platform. Third-party connectors need a predictable bridge between app events and agent execution. If every Slack, GitHub, Linear, Jira, Notion, CRM, support, or commerce adapter invents its own sequence, the platform gets duplicated glue, inconsistent approval behavior, and non-repeatable agent runs.

This compiler makes the sequence explicit and stable. It gives connected applications a previewable contract before any side effects happen and gives a future workflow runner deterministic idempotency keys for each step.

## Provider coverage

The first compiler map covers common integration families inspired by the integration catalog direction and popular service integrations:

- developer/project work: GitHub, Linear, Jira, Trello, Asana, Monday
- collaboration/knowledge: Slack, Microsoft Teams, Discord, Notion, Google Workspace
- support/CRM/commerce: Zendesk, Intercom, HubSpot, Salesforce, Stripe, Shopify
- structured data: Airtable
- fallback: generic

Each provider maps to a required Omnigent capability slug such as `developer.work_item`, `project_management.work_item`, `support.ticket`, `crm.record`, or `commerce.account`.

## Safety and approval model

The compiler defaults to an approval gate for systems where writeback can touch customer, revenue, or system-of-record data:

- Airtable
- HubSpot
- Intercom
- Salesforce
- Shopify
- Stripe
- Zendesk

Callers can override `require_approval` for tenant-specific policy. Callers can also set `writeback=false` for read-only previews and dry-run planning.

## Future unlocks

- Let PR #98's work-item intake attach a compiled plan idempotency key to each task payload.
- Let PR #96's catalog point every capability blueprint at one of these compiled harness shapes.
- Add an execution endpoint that materializes these steps into durable `tool_steps` rows.
- Add provider OAuth installation records and tenant-scoped allowed capabilities.
- Use the plan envelope as the shared contract for Slack slash commands, GitHub issue/PR webhooks, Linear/Jira issue events, Notion database automations, Zendesk/Intercom support tickets, Stripe disputes, and ByteDesk Platform mounted apps.
- Feed outcome records back into routing so future `resolve_assignee` calls choose better specialist agents.

## Test scope

Targeted verification for this surgical feature:

- unit coverage for deterministic key generation, provider capability mapping, approval defaults, writeback disabling, aliases, and validation
- API coverage for `POST /v1/integration-workflow-plans/compile`
- ruff checks for the new module, route, test, and touched extension file
- `git diff --check`

A full repository test run was not necessary for this isolated route/compiler addition.
