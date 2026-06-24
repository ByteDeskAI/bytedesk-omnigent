# Omnigent autonomous feature loop — iteration 10

## Capability implemented

Implemented a built-in Microsoft Teams outgoing-webhook ingress adapter for ByteDesk Omnigent's signed webhook runtime.

New behavior:

- `POST /v1/ingress/microsoft-teams` and `POST /v1/ingress/teams` can resolve a first-party `MicrosoftTeamsWebhookAdapter` without deployment-specific relay glue.
- Teams `Authorization: HMAC <base64-digest>` signatures are verified against the raw request body using HMAC-SHA256 and the existing `OMNIGENT_INGRESS_SECRET_MICROSOFT_TEAMS` / `OMNIGENT_INGRESS_SECRET_TEAMS` secret resolver contract.
- Native Teams outgoing webhook messages route to a stable `message` binding key by default.
- Internal relays can still override routing with `X-Omnigent-Event` when they need more specific bindings such as `teams.incident` or `teams.support`.
- Existing default GitHub/TeamCity-style HMAC behavior and custom adapter registration remain unchanged.

## Prior loop awareness

Before selecting this feature, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- PR #96 / iteration 1: integration capability catalog (`/v1/integration-capabilities`).
- PR #98 / iteration 2: external work-item intake.
- PR #99 / iteration 3: deterministic integration workflow plan compiler.
- PR #100 / iteration 4: connected-app manifest compiler.
- PR #101 / iteration 5: Slack webhook ingress adapter.
- PR #102 / iteration 6: Stripe webhook ingress adapter.
- PR #103 / iteration 7: GitHub webhook event routing.
- PR #104 / iteration 8: JSON payload webhook adapter.
- PR #105 / iteration 9: integration approval plan compiler.

This PR avoids duplicating those open loop branches. It adds a concrete collaboration-channel ingress adapter for Microsoft Teams, which is distinct from Slack, Stripe, GitHub, generic JSON payload routing, manifest compilation, and approval planning.

## Business case

Microsoft Teams is a primary collaboration surface for many enterprises. Native Teams ingress lets ByteDesk Platform and Omnigent deployments wake autonomous agents directly from Teams channels without requiring a bespoke translator service to rewrite Teams signatures into GitHub-style webhook headers.

This supports high-value agent use cases:

- `@omni` incident triage from an operations channel.
- Support and customer-success escalation from Teams into specialist Omnigent agents.
- Project coordination where Teams messages wake deterministic workflow steps or agent sessions.
- Enterprise connected-app onboarding alongside Slack, GitHub, Stripe, and other popular systems.

A built-in adapter reduces connector implementation time and keeps security properties centralized: signed requests, exact binding resolution, durable signal delivery, replay handling through the signal bus, and fail-closed non-2xx responses when no agent is actually woken.

## Future unlocks

- Add Teams Bot Framework / Graph webhook support for richer tenant-scoped installations beyond outgoing webhooks.
- Feed Teams `message` events into external work-item intake once that open PR lands.
- Surface Teams adapter metadata from the integration capability catalog once the catalog PR lands.
- Generate Teams setup instructions from connected-app manifests, including secret name, ingress URL, and default binding key.
- Add Teams writeback tools for threaded replies or adaptive-card approval prompts behind the approval-plan compiler.

## Verification

Targeted verification was run because this iteration only touches the signed ingress adapter seam and its focused tests:

- RED: `PYTHONPATH="$PWD" uv run --extra dev python -m pytest tests/ingress/test_ingress.py::test_microsoft_teams_adapter_verifies_authorization_hmac_and_routes_message tests/ingress/test_ingress.py::test_resolve_webhook_adapter_has_builtin_microsoft_teams_adapter tests/ingress/test_ingress.py::test_process_inbound_delivers_microsoft_teams_message_to_signal_bus -q` failed before implementation with `ImportError: cannot import name 'MicrosoftTeamsWebhookAdapter'`.
- GREEN: the same targeted test command passed after adding the Teams adapter and registry aliases.
- Final targeted commands are recorded in the PR body.
