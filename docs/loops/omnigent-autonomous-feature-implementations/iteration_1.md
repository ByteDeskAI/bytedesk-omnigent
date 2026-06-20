# Autonomous feature loop iteration 1 — integration capability catalog

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_1`

## Capability delivered

Iteration 1 adds a first-party, read-only integration capability catalog exposed
through the ByteDesk Omnigent extension:

- `GET /v1/integration-capabilities`
- `GET /v1/integration-capabilities/{slug}`

The catalog ranks high-value third-party and workflow-harness capabilities that
advance Omnigent's mission as AI agent management middleware: creating,
managing, coordinating, and integrating autonomous agents into external systems.

The initial catalog includes:

1. Archon-style deterministic workflow blueprints
2. Slack command center
3. Linear/Jira work intake
4. GitHub engineering copilot
5. Google Workspace operator
6. Notion knowledge operator
7. Zendesk/Intercom support desk
8. HubSpot/Salesforce CRM agent
9. Trello task bridge
10. Stripe/Shopify revenue ops

Each entry includes:

- detailed implementation description
- future unlocks
- business case
- auth model
- required scopes
- agent value
- product priority score
- external references

## Why this is high value

Omnigent needs a repeatable way to decide which integration surfaces should be
built next and how each maps to agent value. Before this iteration, that logic
lived in ad-hoc planning conversations. The catalog makes integration strategy a
runtime-visible product surface that platform UI, planning agents, and future
workflow generators can query.

This is especially useful for autonomous iteration loops: every future iteration
can inspect the catalog, choose the highest-value unimplemented capability, and
avoid duplicating existing work.

## Implementation description

- Added `bytedesk_omnigent.integration_capabilities` as a typed static catalog.
- Added `bytedesk_omnigent.routes.integration_capabilities` with list/detail
  endpoints mounted by `BytedeskExtension` under `/v1`.
- Kept the surface read-only and deterministic: no secrets, no OAuth live calls,
  no network dependency, and no new database migration.
- Followed existing ByteDesk extension route conventions: multi-user mode can
  require auth via the shared `require_user` helper; single-user/local mode stays
  open.
- Added unit/API tests that verify priority ordering, required strategic fields,
  filtering, detail lookup, and 404 behavior.

## Future unlocks

This capability unlocks the next implementation waves:

1. A platform/Office UI panel that shows ranked integration opportunities.
2. A planning agent that picks the next connector based on priority and existing
   open PRs.
3. A typed `IntegrationAdapter` / `WorkItemAdapter` seam for OAuth connectors.
4. Archon-style workflow-blueprint compilation into Omnigent Tasks and tool
   steps.
5. Marketplace packaging where connectors and workflow templates are discovered,
   scored, and enabled per tenant.
6. Automated gap analysis: compare catalog entries against installed extension
   routers/tools to identify missing capabilities.

## Business case

This shifts Omnigent from a generic agent runtime toward a productized agent
middleware platform. Customers care less about raw agent execution and more about
whether agents can safely operate in the systems where work already happens:
Slack, Jira, GitHub, Google Workspace, Notion, CRMs, support desks, and commerce
systems.

By publishing implementation descriptions, future unlocks, and business cases in
one canonical endpoint, Omnigent can support executive prioritization, product UI,
and autonomous implementation loops with the same source of truth.
