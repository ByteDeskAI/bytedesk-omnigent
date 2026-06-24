# Iteration 40: ServiceNow webhook ingress adapter

## Capability shipped

Added a built-in `ServiceNowWebhookAdapter` to Omnigent's inbound webhook ingress layer.

ServiceNow incident/change events can now enter Omnigent through the existing `POST /v1/ingress/{source}` route by using either:

- `source=servicenow`
- `source=service-now`

The adapter verifies a ServiceNow-specific HMAC signature header, extracts a ServiceNow-specific event routing key, and then reuses the durable `(source, match_key) -> signal_id` binding flow to wake parked autonomous agent sessions.

## Prior loop awareness

Before choosing this work, I inspected open PRs in `ByteDeskAI/bytedesk-omnigent` with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- integration catalog / manifests / workflow planning foundations;
- external work item intake;
- OAuth state and authorize URL helpers;
- activation gates, approval, replay, handoff, rollback, task brief, workflow harness, and webhook probe compilers;
- webhook binding APIs and adapter manifest surfaces;
- provider adapters for Slack, Stripe, GitHub routing, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents/Salesforce, and Monday;
- generic JSON-payload/HMAC adapter surfaces, including Notion coverage in iteration 8.

I explicitly avoided Notion after inspecting iteration 8 because that open PR already registers a `notion` alias through the JSON-payload adapter. ServiceNow was not represented in the open loop PR list, so this iteration adds one non-duplicative enterprise integration capability.

## Implementation details

Changed `bytedesk_omnigent/ingress.py`:

- Added `ServiceNowWebhookAdapter` implementing the existing `WebhookSourceAdapter` protocol.
- Verification reads `X-ServiceNow-Signature` and accepts the existing bare hex or `sha256=<hex>` digest forms through `verify_hmac_signature`.
- Verification also accepts `X-Omnigent-Signature` so ByteDesk Platform or an integration gateway can normalize captured ServiceNow events before forwarding them into Omnigent.
- Routing reads `X-ServiceNow-Event` as the binding `match_key`.
- Missing event headers fall back to `*`, preserving the existing catch-all binding behavior.
- Registered built-in aliases `servicenow` and `service-now` in the webhook adapter registry.

Changed `tests/ingress/test_ingress.py`:

- Added TDD coverage for ServiceNow signature verification, case-insensitive headers, prefixed signatures, normalized Omnigent signature fallback, failed/missing signatures, event extraction, catch-all fallback, and registry alias resolution.

## Business case

ServiceNow is a core enterprise ITSM and workflow system. Many customer operations begin as ServiceNow incidents, change approvals, service requests, or escalation records. A first-party ingress adapter lets Omnigent agents react to those events without custom route glue per deployment.

Concrete unlocks:

- Wake an incident response agent when a critical incident changes state.
- Trigger a change-risk review agent when a change request is approved or scheduled.
- Route service desk events into ByteDesk/Omnigent managed workflows.
- Expand Omnigent's integration story from developer/project tools into enterprise IT operations.

## Future unlocks

- Add a ServiceNow entry to `/v1/integration-capabilities` once the catalog branch lands.
- Add ByteDesk Platform UI affordances for creating ServiceNow incident/change bindings.
- Add payload-aware routing if product wants to derive match keys from native ServiceNow JSON fields when no gateway-stamped `X-ServiceNow-Event` header is present.
- Add an OAuth/connected-app manifest for ServiceNow API scopes and instance URLs.
- Extend the deterministic webhook probe compiler with a ServiceNow preset once the probe branch lands.

## Verification

TDD RED run before implementation:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_servicenow_adapter_verifies_signature_and_reads_event tests/ingress/test_ingress.py::test_resolve_webhook_adapter_registers_servicenow_builtin -q
```

Result: failed with `ImportError: cannot import name 'ServiceNowWebhookAdapter'`, confirming the tests described missing behavior.

Targeted GREEN run after implementation:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_servicenow_adapter_verifies_signature_and_reads_event tests/ingress/test_ingress.py::test_resolve_webhook_adapter_registers_servicenow_builtin -q
```

Result: `2 passed, 1 warning in 0.13s`.

Additional verification run before PR completion:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py
git diff --check
```

Result: full ingress target `9 passed, 1 warning in 0.83s`; `ruff check` reported `All checks passed!`; `git diff --check` produced no whitespace errors.

The warning is the existing `tests/known_failures.yaml` unmatched-entry warning emitted by targeted collection.
