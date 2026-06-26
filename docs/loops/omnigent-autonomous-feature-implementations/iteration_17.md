# Omnigent autonomous feature loop — iteration 17

## Capability shipped

Iteration 17 adds a deterministic webhook ingress preflight harness for connected-app onboarding.

New surface:

- `POST /v1/ingress/{source}/preview`
- Pure preflight function: `bytedesk_omnigent.ingress.preview_inbound`

The preview path verifies a signed provider event using the same secret resolver and per-source webhook adapter registry as production delivery, resolves the configured `(source, match_key) -> signal_id` binding, and returns a deterministic result without calling `bus.deliver` or waking the parked agent.

This gives ByteDesk Platform and future integration installers a safe setup-test endpoint for providers such as Zendesk, Intercom, HubSpot, Jira, Asana, Airtable, Discord, and other webhook-backed apps before autonomous writeback or event routing is enabled.

## Prior loop awareness

Before selecting this capability, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- #96 / iteration 1: integration capability catalog.
- #98 / iteration 2: external work-item intake.
- #99 / iteration 3: integration workflow plan compiler.
- #100 / iteration 4: connected-app installation manifest compiler.
- #101 / iteration 5: Slack webhook ingress adapter.
- #102 / iteration 6: Stripe webhook ingress adapter.
- #103 / iteration 7: GitHub webhook event routing.
- #104 / iteration 8: JSON payload webhook adapter.
- #105 / iteration 9: integration approval plan compiler.
- #106 / iteration 10: Microsoft Teams webhook ingress adapter.
- #107 / iteration 11: Linear webhook ingress adapter.
- #108 / iteration 12: Shopify webhook ingress adapter.
- #109 / iteration 13: webhook binding management API.
- #110 / iteration 14: integration event route compiler.
- #111 / iteration 15: integration secret readiness plans.
- #112 / iteration 16: integration OAuth state tokens.

This iteration avoids duplicating provider-specific adapters, catalog entries, manifest compilers, approval compilers, OAuth state tokens, or binding-management APIs. It fills the operational gap between “binding exists” and “turn on live autonomous delivery”: deterministic, no-side-effect proof that the real signed event will route to the intended parked signal.

## Implementation details

- Adds `preview_inbound(...)` to `bytedesk_omnigent.ingress`.
  - Uses the same `WebhookSourceAdapter` contract as `process_inbound`.
  - Verifies the raw request body against the resolved secret.
  - Extracts the match key through the adapter.
  - Resolves the binding through `IngressBindingStore`.
  - Returns `401` for bad signatures, `404` for missing bindings, and `200` for a matched preflight.
  - Never calls the signal bus, so pending waits remain parked.
- Adds `POST /v1/ingress/{source}/preview` in `bytedesk_omnigent.routes.ingress`.
  - Uses `resolve_secret(source)` and `resolve_webhook_adapter(source)` exactly like the production `/v1/ingress/{source}` route.
  - Returns the same response shape (`status`, `signal_id`, `detail`) with preview-specific detail on success.
- Adds targeted tests proving successful preflight does not deliver and error paths remain fail-closed.

## Business case

Connected-app marketplaces need an install-time confidence check. Without a preview harness, a Platform wizard or operator must choose between manual inspection and firing a real webhook that wakes an autonomous workflow. That is risky for customer-facing apps: a Zendesk ticket, HubSpot contact, Jira issue, or Airtable record could trigger work before the integration owner has approved the route.

This preflight endpoint makes onboarding safer and more supportable:

1. The installer creates or selects a parked signal/binding.
2. The provider sends a signed test event, or Platform replays a captured setup payload.
3. Omnigent verifies the signature and resolves the exact signal target.
4. Platform shows “ready to enable” without waking the agent.
5. Only after user approval does live `/v1/ingress/{source}` delivery begin.

That directly improves Omnigent’s mission of autonomous agent coordination and third-party application integration by reducing integration setup failures and preventing accidental autonomous execution during install.

## Future unlocks

- Platform integration UI can render a “Send test event” readiness step for every webhook-backed connected app.
- Provider-specific adapters can expose sample preview payloads and event labels.
- Binding-management APIs can include a one-click preview action after creating a binding.
- Approval-plan compilers can require a successful preview before enabling autonomous writeback.
- Archon-style deterministic workflow harnesses can use preview results as a gate before executing multi-step provider workflows.

## Test plan

Targeted verification run from the managed iteration 17 worktree:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_preview_inbound_verifies_and_resolves_without_delivering tests/ingress/test_ingress.py::test_preview_inbound_reports_bad_signature_and_missing_binding -q`

Result: `2 passed, 1 warning`.

Additional verification before PR:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py bytedesk_omnigent/routes/ingress.py tests/ingress/test_ingress.py`
  - Result: `All checks passed!`
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py tests/ingress/test_secret_resolver_seam.py -q`
  - Result: `13 passed, 1 warning`.
- `git diff --check`
  - Result: passed with no whitespace errors.
