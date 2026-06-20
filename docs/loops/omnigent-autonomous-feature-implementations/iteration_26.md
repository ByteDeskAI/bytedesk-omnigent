# Iteration 26 — Asana webhook ingress adapter

## Capability shipped

Adds a first-class Asana webhook source adapter for Omnigent's signed inbound event ingress pipeline. `POST /v1/ingress/asana` can now authenticate Asana-style webhook deliveries with `X-Hook-Signature` HMAC-SHA256 and route them through Omnigent's existing durable signal-bus wakeup path.

## Prior loop awareness

Before selecting this work, I inspected the open autonomous loop PRs matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #121 iteration 25: Zendesk webhook ingress adapter
- #120 iteration 24: Trello webhook ingress adapter
- #119 iteration 23: Discord ingress signature adapter
- #118 iteration 22: integration workflow harness compiler
- #117 iteration 21: integration OAuth authorize URL compiler
- #116 iteration 20: integration activation gates
- #115 iteration 19: integration handoff package compiler
- #114 iteration 18: integration replay plan compiler
- #113 iteration 17: webhook ingress preflight preview
- #112 iteration 16: integration OAuth state tokens
- #111 iteration 15: integration secret readiness plans
- #110 iteration 14: integration event route compiler
- #109 iteration 13: webhook binding management API
- #108 iteration 12: Shopify webhook ingress adapter
- #107 iteration 11: Linear webhook ingress adapter
- #106 iteration 10: Microsoft Teams webhook ingress adapter
- #105 iteration 9: integration approval plan compiler
- #104 iteration 8: JSON payload webhook adapter
- #103 iteration 7: GitHub webhook events
- #102 iteration 6: Stripe webhook ingress adapter
- #101 iteration 5: Slack webhook ingress adapter
- #100 iteration 4: connected app manifest compiler
- #99 iteration 3: integration workflow plan compiler
- #98 iteration 2: external work item intake
- #96 iteration 1: integration capability catalog

Asana was not covered by the open loop PR set, and it is a natural next integration for autonomous work intake and project/task coordination.

## Implementation details

- Added `AsanaWebhookAdapter` in `bytedesk_omnigent/ingress.py`.
- The adapter verifies `X-Hook-Signature` against the raw body using the existing constant-time HMAC helper.
- Signature values are accepted in bare hex or `sha256=<hex>` form for consistency with the existing ingress helper.
- The adapter reads `X-Asana-Event` as an optional ByteDesk/edge-proxy routing header and falls back to the per-source `"*"` catch-all binding when absent.
- The built-in webhook adapter registry now registers `asana`, so `resolve_webhook_adapter("asana")` works without deployment-specific bootstrap code.
- Added a focused unit test that drove the implementation test-first.

## Business case

Asana is a common operating system for teams, campaigns, customer work, and implementation projects. First-class Asana ingress lets Omnigent agents react to task/project changes where teams already plan work:

- Create or wake autonomous agents from Asana task updates.
- Coordinate follow-up work when milestones, dependencies, or custom fields change.
- Reduce manual copy/paste between project management systems and ByteDesk/Omnigent agent work queues.
- Make Omnigent easier to embed into third-party applications used by non-technical teams.

This directly advances Omnigent's mission around autonomous agent coordination and third-party application integration.

## Future unlocks

- Add an Asana challenge/handshake route helper for initial webhook registration flows.
- Compile Asana `events[]` payload entries into deterministic match keys such as `task.changed`, `project.added`, or `story.created` without relying on an edge-proxy header.
- Add a connected-app manifest for Asana scopes, webhook setup instructions, and activation gates once the open catalog/activation PRs land.
- Add OAuth token exchange and workspace/project discovery for deeper Asana agent actions.

## Test plan

Targeted verification performed:

- RED: `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_asana_adapter_verifies_x_hook_signature_and_reads_event -q` failed because `AsanaWebhookAdapter` did not exist yet.
- GREEN: same targeted test passed after adding the adapter and registry entry.

Additional verification before PR:

- Run `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q`.
- Run `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py`.
- Run `git diff --check`.
