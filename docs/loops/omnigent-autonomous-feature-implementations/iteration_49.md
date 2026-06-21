# Iteration 49: Sentry webhook ingress adapter

## Capability shipped

Added a source-native `SentryWebhookAdapter` for `POST /v1/ingress/sentry`.

The adapter verifies Sentry-style webhook signatures from `Sentry-Hook-Signature` against the raw request body and maps `Sentry-Hook-Resource` to Omnigent's durable ingress binding `match_key`. This lets teams bind resources such as `issue`, `error`, or `metric_alert` to parked remediation, triage, escalation, and customer-response agents without writing custom glue code.

## Prior loop awareness

Before choosing this work, I inspected the open loop PRs matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*` and found iterations 1 through 48 already covering the integration catalog, workflow/route/approval/replay/rate-limit/OAuth compilers, and webhook adapters for Slack, Stripe, GitHub, Microsoft Teams, Shopify, Linear, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, and Bitbucket.

Sentry was not represented in those open loop branches, and it directly extends Omnigent's mission by letting autonomous agents react to production errors and alert resources from a widely deployed engineering observability tool.

## Implementation details

- Added `SentryWebhookAdapter` in `bytedesk_omnigent/ingress.py`.
- Registered `sentry` in the existing webhook source adapter registry so `resolve_webhook_adapter("sentry")` is source-native while unknown sources still fall back to the GitHub-compatible default adapter.
- Kept the change surgical: no schema changes, no secrets handling changes, and no route surface changes. Existing `/v1/ingress/{source}` glue already resolves the per-source adapter and passes the configured secret into `process_inbound`.
- Added a targeted test proving signature verification, optional `sha256=` prefix compatibility, missing/bad signature rejection, resource-to-match-key routing, and catch-all fallback.

## Business case

Sentry is a natural trigger source for autonomous operations agents. With this adapter, ByteDesk/Omnigent can support workflows such as:

- wake an incident-response agent when a `metric_alert` webhook arrives;
- dispatch a code-investigation agent when an `issue` resource is created or regressed;
- notify customer-success or support agents when production errors affect enterprise accounts;
- route different Sentry resources to different departments through existing ingress bindings.

This improves the integration story for engineering teams and demonstrates that Omnigent can turn operational telemetry into governed autonomous work.

## Future unlocks

- Add a Sentry manifest/catalog entry once the integration capability catalog lands on `develop`.
- Add a richer payload normalizer that derives match keys from action + resource, for example `issue.created` or `metric_alert.resolved`.
- Add a deterministic workflow harness scenario that simulates a Sentry alert, verifies the matching binding, and asserts the assigned remediation agent receives the normalized incident brief.
- Add UI affordances in ByteDesk Platform for creating Sentry ingress bindings and storing the signing secret in the platform secret manager.

## Test plan

- RED: `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_sentry_adapter_verifies_hook_signature_and_routes_resource -q` failed before implementation with `ImportError: cannot import name 'SentryWebhookAdapter'`.
- GREEN: the same targeted test passed after implementation.
- Final verification also ran the full ingress test module, Ruff on touched Python files, and `git diff --check`.
