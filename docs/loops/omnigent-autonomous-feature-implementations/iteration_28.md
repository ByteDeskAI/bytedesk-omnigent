# Iteration 28 — Jira webhook ingress adapter

## Capability shipped

Added a built-in Jira webhook ingress adapter so Omnigent can wake parked agent sessions from Jira issue/project automation events.

The adapter:
- Registers `jira` as a first-class webhook source in the pluggable webhook adapter registry.
- Verifies signed inbound Jira webhook requests with the same HMAC-SHA256 shared-secret contract used by Omnigent ingress deployments (`X-Omnigent-Signature` or `X-Hub-Signature-256`).
- Routes bindings from Jira's JSON payload event field, `webhookEvent` (for example `jira:issue_created`), with header fallbacks for gateway-normalized events.
- Keeps backward compatibility for existing custom adapters that still implement the original `match_key(headers)` shape.

This means a ByteDesk Platform or customer deployment can register a binding like:

- source: `jira`
- match_key: `jira:issue_created`
- signal_id: a durable Omnigent signal awaited by an autonomous delivery, support, or project-management agent

Then `POST /v1/ingress/jira` can deliver the event to the signal bus and resume the correct parked session.

## Prior loop awareness

Before selecting this capability, I inspected open loop PRs targeting branches `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- Iterations 1–4 covered the integration capability catalog, external work intake, workflow plan compilation, and connected app manifests.
- Iterations 5–13 added Slack, Stripe, GitHub, JSON payload, approval, Microsoft Teams, Linear, Shopify, and webhook binding management surfaces.
- Iterations 14–22 added event route, secret readiness, OAuth state, preflight preview, replay/handoff/activation/OAuth URL, and workflow harness compilers.
- Iterations 23–27 added Discord, Trello, Zendesk, Asana, and HubSpot webhook ingress adapters.

Jira was not yet covered by the open loop PR set, and it is a high-value integration for ByteDesk delivery automation because many ByteDesk agents already create, triage, or coordinate Jira work.

## Implementation details

Changed files:

- `bytedesk_omnigent/ingress.py`
  - Added `JiraWebhookAdapter`.
  - Registered `jira` in the webhook adapter registry.
  - Made adapter match-key resolution payload-aware while preserving compatibility with older one-argument adapters.
- `tests/ingress/test_ingress.py`
  - Added a full signal-bus delivery test proving a Jira `webhookEvent` payload routes to the matching binding and wakes a parked wait.
  - Added a registry test proving `resolve_webhook_adapter("jira")` returns the built-in Jira adapter.

## Business case

Jira is one of the core systems of record for software delivery. First-class Jira webhook ingress lets Omnigent agents react immediately when work changes in ByteDesk Platform or a customer's Atlassian workspace:

- issue created → route to intake/triage agent,
- status changed → wake delivery sequencing or escalation agents,
- comment added → resume a customer-support or engineering-assistant flow,
- priority/assignee changed → trigger workload rebalance or incident coordination.

This turns Omnigent from a chat/request-driven agent runtime into an event-driven agent workforce that can sit behind ByteDesk Platform and customer Jira instances.

## Future unlocks

- Add a Jira connected-app manifest and setup checklist so tenants can self-serve binding creation.
- Add Jira OAuth installation flow once the integration OAuth compiler lands in develop.
- Add schema-aware event previews for common Jira payloads (`issue_created`, `issue_updated`, comments, sprint events).
- Add an optional replay harness fixture for deterministic Jira event testing across agent workflows.
- Extend the same payload-aware adapter path to Notion, Intercom, and Salesforce events where the event type is also body-carried.

## Test plan

Targeted tests run:

- RED check before implementation:
  - `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_jira_adapter_routes_on_webhook_event_payload tests/ingress/test_ingress.py::test_jira_adapter_is_registered_builtin_source -q`
  - Expected failure observed: `ImportError: cannot import name 'JiraWebhookAdapter'`.
- GREEN targeted check:
  - `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_jira_adapter_routes_on_webhook_event_payload tests/ingress/test_ingress.py::test_jira_adapter_is_registered_builtin_source -q`
  - Result: `2 passed, 1 warning`.
- Ingress regression check:
  - `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q`
  - Result: `9 passed, 1 warning`.
- Targeted lint:
  - `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py`
  - Result: `All checks passed!`.
- Whitespace check:
  - `git diff --check`
  - Result: passed with no output.

The pytest warning is pre-existing known-failures metadata noise from `tests/conftest.py` and is not introduced by this change.
