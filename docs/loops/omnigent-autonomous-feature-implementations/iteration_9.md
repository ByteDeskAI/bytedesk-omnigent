# Omnigent autonomous feature loop — iteration 9

## Capability implemented

Iteration 9 adds a deterministic connected-app approval-plan compiler for ByteDesk Omnigent.

New surface:

- `POST /v1/integration-approval-plans/compile`
- Pure compiler module: `bytedesk_omnigent.integration_approval`
- Route module: `bytedesk_omnigent.routes.integration_approval`

The compiler takes a provider slug, requested OAuth/app scopes, planned autonomous operations, and an optional `writeback_enabled` flag. It returns a stable approval plan containing:

- normalized provider and scopes;
- low/medium/high/critical risk classification;
- required approval level: `none`, `user`, `admin`, or `two_key`;
- deterministic gates such as scope preview, installer consent, workspace admin approval, second reviewer approval, dry-run before writeback, and audit logging;
- readonly/write/admin scope buckets;
- reason codes for audit trails and Platform UI copy;
- a stable idempotency key for preview/install retries;
- a ByteDesk mount hint for integration setup flows.

## Prior loop awareness

Before choosing this feature, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- PR #96 / iteration 1: integration capability catalog (`/v1/integration-capabilities`).
- PR #98 / iteration 2: external work item intake.
- PR #99 / iteration 3: integration workflow plan compiler.
- PR #100 / iteration 4: connected app manifest compiler.
- PR #101 / iteration 5: Slack webhook ingress adapter.
- PR #102 / iteration 6: Stripe webhook ingress adapter.
- PR #103 / iteration 7: GitHub webhook event routing.
- PR #104 / iteration 8: JSON payload webhook adapter.

This iteration avoids duplicating those open branches. Instead of adding another catalog entry, intake normalizer, manifest compiler, or webhook adapter, it adds the missing pre-install governance layer that Platform and connector setup flows need before Omnigent can safely receive tokens or write back to third-party systems.

## Business case

Autonomous agents become more valuable when they can act inside systems of record such as GitHub, Google Workspace, HubSpot, Salesforce, Stripe, Shopify, Zendesk, Jira, Linear, Slack, and Notion. Those integrations also create trust and liability risk: a connector that can update CRM records, send emails, create GitHub issues, or refund payments needs predictable human gates before activation.

This capability gives ByteDesk Platform a deterministic approval preview it can show before OAuth installation or service-token enablement. That reduces integration setup friction, standardizes customer-facing risk language, and creates an audit-ready contract for autonomous writeback.

## Future unlocks

- Feed connected-app manifest compiler output directly into this approval compiler before installation.
- Persist approval plans with installer identity, reviewer identity, and token grant metadata.
- Enforce the returned gates at runtime through policy modules before writeback tools execute.
- Add provider-specific scope dictionaries for richer UI explanations.
- Surface approval previews in ByteDesk Office integration setup screens.

## Verification

Targeted verification run for this surgical change:

- `uv run --with pytest --with pytest-asyncio --with httpx pytest tests/test_integration_approval.py -q`
- `uv run --extra dev ruff check bytedesk_omnigent/integration_approval.py bytedesk_omnigent/routes/integration_approval.py tests/test_integration_approval.py`

The targeted test covers pure compiler behavior for readonly, admin/writeback, and system-of-record cases plus the FastAPI route success and validation paths.
