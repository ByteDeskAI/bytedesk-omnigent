# Iteration 37 — Deterministic webhook smoke-test probe

## Capability shipped

Added a small, deterministic webhook probe compiler plus a ByteDesk extension route:

- Pure compiler: `bytedesk_omnigent.integration_probe.compile_webhook_probe`
- API route: `POST /v1/integration-probes/webhook`
- Extension mount: `BytedeskExtension.routers()` now exposes the route under `/v1`

Given a source, match key, secret, and either canonical JSON payload or a raw provider-captured body, Omnigent now returns:

- the exact ingress URL (`/v1/ingress/{source}`),
- canonical body bytes (or raw-body replay),
- `x-omnigent-event` and `x-omnigent-signature` headers,
- a copy/pasteable `curl` command, and
- expected status explanations for 202/401/404/409/410.

This is intentionally a smoke-test harness, not another provider-specific adapter. It helps operators validate an ingress binding and parked signal before enabling production webhook delivery from Slack, GitHub, Linear, Jira, Notion, Google Workspace, Salesforce, HubSpot, Zendesk, or any other provider that can forward into Omnigent's signed ingress surface.

## Prior loop awareness

Before choosing this work I inspected open loop PRs with branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Relevant already-open work included provider adapters and integration compilers:

- PR #96 iteration 1: integration capability catalog
- PR #98 iteration 2: external work item intake
- PR #99 iteration 3: integration workflow plan compiler
- PR #100 iteration 4: connected app manifest compiler
- PRs #101–#132: Slack/GitHub/Stripe/Teams/Linear/Shopify/Discord/Trello/Zendesk/Asana/HubSpot/Jira/Intercom/GitLab/Google Workspace/Airtable webhook adapters plus OAuth/state/activation/replay/rollback/task-brief helpers

To avoid duplicating provider adapter work, iteration 37 adds a deterministic activation-time probe surface that complements all of those adapters and the existing signed ingress path.

## Implementation details

### Pure compiler

`compile_webhook_probe(...)`:

- requires exactly one of `payload` or `raw_body`,
- canonicalizes JSON payloads with sorted compact JSON for repeatable signatures,
- preserves raw provider captures byte-for-byte when `raw_body` is supplied,
- signs the body with the same HMAC-SHA256 convention used by the default ingress adapter,
- builds lower-case ingress headers that work with the existing case-insensitive adapter lookup, and
- returns a frozen `WebhookProbe` dataclass.

### API route

`POST /v1/integration-probes/webhook`:

- requires auth in multi-user mode because the request includes a webhook secret,
- remains open in single-user/local-dev mode like sibling ByteDesk routes,
- rejects ambiguous body sources with HTTP 422, and
- never stores or logs secrets; it only returns the derived one-time probe payload.

Example request:

```json
{
  "source": "github",
  "match_key": "issues.opened",
  "secret": "whsec_test",
  "payload": {"action": "opened"},
  "base_url": "https://omnigent.example.com/v1"
}
```

## Business case

Webhook integrations fail most often at activation time: wrong URL, wrong secret, event mismatch, missing binding, expired wait, or replay. Those failures are expensive because they happen while an external SaaS is already configured and may be retrying production events.

This capability gives ByteDesk/Omnigent operators a deterministic preflight:

1. create or inspect the binding,
2. call `/v1/integration-probes/webhook`,
3. paste the generated `curl`, and
4. confirm the ingress path returns the expected status before enabling the third-party webhook.

That shortens integration onboarding, reduces support churn, and makes autonomous agents safer to connect to customer systems.

## Future unlocks

- Add provider-specific probe strategies that reuse the webhook adapter registry once provider adapters land from the open loop PRs.
- Add an optional `binding_id`/`signal_id` lookup mode so the route can compile probes directly from durable binding records without requiring the caller to retype source/event values.
- Surface this in the ByteDesk Platform UI as a "Test webhook" button on connected app setup screens.
- Emit a structured activation report that can be attached to integration handoff/approval plans.

## Test plan

Targeted verification run from the iteration 37 worktree:

```bash
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py tests/ingress/test_integration_probe.py tests/server/routes/test_integration_probe_route.py -q
```

Result: `12 passed, 1 warning in 0.80s`.

The warning is the repository's existing `tests/known_failures.yaml` unmatched-entry warning and is unrelated to this change.

Also run before PR creation:

```bash
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_probe.py bytedesk_omnigent/routes/integration_probe.py tests/ingress/test_integration_probe.py tests/server/routes/test_integration_probe_route.py
git diff --check
```
