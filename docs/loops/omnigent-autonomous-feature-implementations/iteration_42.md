# Iteration 42: deterministic integration rate-limit plan compiler

## Capability shipped

Added a small, deterministic compiler for third-party integration rate-limit plans:
`bytedesk_omnigent.integration_rate_limits.compile_rate_limit_plan`.

The compiler turns an integration service, operation, idempotency fields, and an optional
`IntegrationRateLimitProfile` into a JSON-serializable envelope that autonomous agents can
carry before writing into systems such as Slack, Notion, Linear, Jira, HubSpot, Salesforce,
Zendesk, Intercom, Google Workspace, Microsoft Teams, Discord, Asana, Monday, Airtable,
Shopify, Stripe, or ByteDesk Platform.

The plan includes:

- normalized service slug and operation name
- retryable HTTP statuses, sorted deterministically
- bounded max attempts
- exponential-jitter backoff settings
- a stable idempotency key template
- agent instructions for `Retry-After`, no-double-fire retries, and human escalation

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs with head branches matching
`feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers many webhook adapters and integration compilers,
including Slack, Stripe, GitHub webhook routing, Teams, Linear, Shopify, webhook bindings,
OAuth state/authorize URL, replay/handoff/rollback/task brief/probe compilers, and multiple
service-specific ingress adapters through Salesforce.

This iteration does not add another webhook adapter and does not duplicate those open PRs.
Instead, it fills a cross-cutting safety gap for outbound side-effecting integration calls:
agents now have a deterministic way to carry retry and idempotency policy before invoking
external APIs.

## Implementation details

Files changed:

- `bytedesk_omnigent/integration_rate_limits.py`
  - Added `IntegrationRateLimitProfile` dataclass.
  - Added `compile_rate_limit_plan(...)` with validation, service slug normalization,
    sorted retry statuses, stable idempotency key templates, and JSON-serializable output.
- `tests/test_integration_rate_limits.py`
  - Covers default Slack-style plan output.
  - Covers custom profiles and deterministic retry-status sorting.
  - Covers blank service/operation validation.
  - Covers required idempotency fields.

This module is intentionally SDK-free and secret-free so it can be embedded in manifests,
agent task briefs, handoff packages, workflow harnesses, or ByteDesk Platform integration
plans without pulling provider dependencies into core execution.

## Business case

Third-party integrations create revenue only when agents can safely take actions in real
customer systems. The riskiest path is a write action that times out or rate-limits and then
gets retried without a stable idempotency strategy.

This capability gives Omnigent a reusable, deterministic contract for outbound integration
writes. That directly improves reliability for marketplace agents, enterprise connectors,
and ByteDesk Platform workflows by reducing duplicate side effects, making retry behavior
reviewable, and creating a clear escalation point when automation exhausts safe retries.

## Future unlocks

- Attach these plans to integration capability catalog entries once the catalog lands on
  `develop`.
- Surface compiled plans in ByteDesk task briefs so human reviewers can approve outbound
  write behavior before activation.
- Add service-specific default profiles for Slack, Notion, GitHub, Linear, Jira, HubSpot,
  Salesforce, Zendesk, Intercom, Stripe, Shopify, Microsoft Teams, Discord, Asana, Monday,
  Airtable, and Google Workspace as those connectors mature.
- Use the idempotency key template in webhook/event-route and workflow-harness compilers so
  inbound events and outbound writes share one deterministic no-double-fire story.

## Verification

TDD red step:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_rate_limits.py -q`
- Expected failure observed before implementation: `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_rate_limits'`.

Green/targeted verification:

- `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_rate_limits.py -q`
- Result: `5 passed, 1 warning in 0.12s`.

The warning is the repo's existing `tests/known_failures.yaml` unmatched-entry warning during
collection; it is unrelated to this new module.
