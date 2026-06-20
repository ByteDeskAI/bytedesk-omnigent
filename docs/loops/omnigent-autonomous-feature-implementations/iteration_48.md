# Iteration 48 — Bitbucket webhook ingress adapter

## Capability shipped

Adds a built-in Bitbucket Cloud webhook ingress adapter for Omnigent's signed inbound webhook pipeline.

A deployment can now configure `POST /v1/ingress/bitbucket` with `OMNIGENT_INGRESS_SECRET_BITBUCKET` and bind Bitbucket event keys such as `repo:push`, `pullrequest:created`, or `pullrequest:fulfilled` to parked Omnigent sessions without custom adapter registration glue.

## Prior loop awareness

Before selecting this feature, I inspected all open loop PRs whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- Recent iterations already cover Notion, Salesforce, ServiceNow, Monday, CloudEvents, Airtable, Google Workspace, GitLab, Intercom, Jira, HubSpot, Asana, Zendesk, Trello, Discord, Shopify, Linear, Microsoft Teams, Stripe, Slack, GitHub routing, and several deterministic integration plan compilers.
- I also searched open PRs for Bitbucket and found no matching open Bitbucket integration work.

This iteration intentionally avoids duplicating those open PRs and adds a complementary source adapter for a high-signal developer workflow integration.

## Implementation details

- Added `BitbucketWebhookAdapter` in `bytedesk_omnigent/ingress.py`.
- The adapter verifies Bitbucket-style HMAC-SHA256 request signatures from `X-Hub-Signature` using the existing constant-time `verify_hmac_signature` helper.
- It accepts both `sha256=<hex>` and bare digest forms, matching the defensive behavior of the existing GitHub/default adapter.
- It routes events by reading Bitbucket's `X-Event-Key` header, falling back to `*` so catch-all bindings still work.
- Registered `bitbucket` as a built-in webhook source in the existing pluggable webhook adapter registry.
- Added targeted ingress tests proving signature verification, event-key extraction, bad-signature rejection, catch-all fallback, and registry resolution.

## Business case

Bitbucket remains common in enterprises that already use Atlassian products. First-class Bitbucket ingress lets Omnigent agents react to code and pull-request lifecycle events from those organizations without requiring bespoke Python registration in each deployment.

Near-term use cases:

- Auto-create or resume release agents when `repo:push` lands on protected branches.
- Wake review, QA, or deployment agents when `pullrequest:created` or `pullrequest:fulfilled` fires.
- Support Atlassian-heavy customers alongside Jira and GitLab/GitHub-style developer workflows.

This directly advances Omnigent's mission of autonomous agent coordination and third-party application integration.

## Future unlocks

- Add a declarative Bitbucket binding template that maps common event keys to recommended agent workflows.
- Enrich delivered payloads with normalized repository, branch, pull request, and actor fields so downstream agents can be source-agnostic.
- Pair Bitbucket webhook ingress with OAuth/App Password onboarding for agent-initiated Bitbucket API actions.
- Add deterministic workflow harness examples for Bitbucket PR review and release orchestration flows.

## Verification

Targeted red/green tests were run with the canonical virtualenv from the managed worktree:

- RED: `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_bitbucket_adapter_verifies_hmac_and_reads_event_key tests/ingress/test_ingress.py::test_resolve_webhook_adapter_has_builtin_bitbucket_adapter -q`
  - Failed as expected with `ImportError: cannot import name 'BitbucketWebhookAdapter'` before implementation.
- GREEN: same targeted command after implementation.
  - Passed: `2 passed, 1 warning`.

Additional verification performed after implementation:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q`
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py`
- `git diff --check`

Full suite was not run because this is a surgical ingress adapter change covered by the focused ingress test module and lint/diff checks.
