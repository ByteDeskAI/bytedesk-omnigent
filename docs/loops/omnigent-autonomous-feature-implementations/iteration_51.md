# Iteration 51: Connected-App OAuth Scope Review

## Capability shipped

Iteration 51 adds a deterministic OAuth scope review surface for connected-app setup:

- Pure compiler: `bytedesk_omnigent.integration_scope_review.review_integration_scopes`
- API route: `POST /v1/integration-scope-review`
- ByteDesk extension mount: `BytedeskExtension.routers()` now exposes the route under `/v1`

Given a service slug and requested OAuth scopes, Omnigent now returns a secret-free install posture:

- normalized and de-duplicated requested scopes,
- approved known scopes,
- high-risk scopes,
- unknown scopes,
- aggregate risk (`low`, `medium`, `high`),
- whether human approval is required,
- explanatory recommendations, and
- governance policy recommendations such as `two_key_approval`, `dry_run_write_actions`, and `least_privilege_scope_trim`.

The built-in catalog covers popular integration targets: Slack, GitHub, Linear, Jira, Notion, Google Workspace, HubSpot, Salesforce, and Zendesk.

## Prior loop awareness

Before choosing this feature I inspected open ByteDeskAI/bytedesk-omnigent PRs whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- PR #96 / iteration 1: integration capability catalog.
- PRs #101-#145: many provider-specific webhook adapters and integration compilers for Slack, Stripe, GitHub, Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, Sentry, plus OAuth authorize/refresh, activation gates, replay/rollback/rate-limit/dead-letter helpers, credential rotation, and task briefs.
- PR #146 / iteration 50: integration agent blueprint previews.

To avoid duplicating those PRs, this iteration does not add another webhook adapter, OAuth URL compiler, credential refresh flow, generic capability catalog, or agent blueprint. Instead it adds the missing pre-install safety check: classify requested OAuth scopes before a third-party connected app is authorized.

## Implementation details

### Pure review compiler

`review_integration_scopes(service, requested_scopes)` is deterministic and side-effect free:

- It performs no provider API calls.
- It reads no credentials or secrets.
- It normalizes the service slug and de-duplicates scope input while preserving order.
- Known low-risk scopes pass without a human gate.
- Medium/high scopes are approved as known but require a stronger governance posture.
- Unknown services and unknown scopes fail closed as high-risk and require manual review.
- High-risk or unknown requests get policy recommendations for two-key approval, dry-run write actions, and least-privilege trimming.

### API route

`POST /v1/integration-scope-review` is mounted through the ByteDesk extension. It follows the existing extension route auth convention:

- Open in single-user/local-dev mode (`auth_provider=None`).
- Requires a resolved user in multi-user mode.

Example request:

```json
{
  "service": "github",
  "requested_scopes": ["read:user", "repo"]
}
```

Example response excerpt:

```json
{
  "service": "github",
  "risk": "high",
  "high_risk_scopes": ["repo"],
  "requires_human_approval": true,
  "policy_recommendations": [
    {"policy": "two_key_approval", "reason": "..."},
    {"policy": "dry_run_write_actions", "reason": "..."}
  ]
}
```

## Business case

Connected-app integrations are valuable because they let autonomous agents operate inside customer systems, but OAuth scope grants are also one of the highest-risk moments in onboarding. A single overbroad scope can expose mailboxes, drives, repositories, CRM records, support queues, or admin configuration.

This capability gives ByteDesk Platform and future autonomous integration agents a stable least-privilege checkpoint before install:

1. Generate or receive a connected-app manifest.
2. Submit requested scopes to Omnigent.
3. Show users a clear risk posture and required approval gate.
4. Start write-capable integrations in dry-run mode until verified.
5. Trim unknown or overbroad scopes before customer authorization.

That reduces support risk, shortens security review cycles, and makes marketplace connected-app agents safer to launch.

## Future unlocks

- Join this route with the open integration capability catalog once iteration 1 lands.
- Let integration agent blueprints include expected scopes and automatically call this review before presenting an install button.
- Add per-tenant allow/deny overrides so customers can tune scope policy by workspace.
- Attach policy recommendations directly to generated session/default policies.
- Extend the catalog to Microsoft Teams, Discord, Stripe, Shopify, Airtable, Monday, Intercom, and Trello OAuth scopes.
- Add an audit event so ByteDesk Platform can show historical scope decisions for compliance review.

## Test plan

TDD red phase:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_scope_review.py -q`
  - Failed as expected with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_scope_review'`.
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/server/routes/test_integration_scope_review_route.py -q`
  - Failed as expected with `ModuleNotFoundError: No module named 'bytedesk_omnigent.routes.integration_scope_review'`.

Verification run:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_scope_review.py tests/server/routes/test_integration_scope_review_route.py -q`
  - Passed: `5 passed, 1 warning in 0.11s`.

Additional verification performed before PR:

- Ruff check on changed Python files.
- `git diff --check`.
