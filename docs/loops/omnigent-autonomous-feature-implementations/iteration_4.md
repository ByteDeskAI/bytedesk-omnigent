# Omnigent autonomous feature loop — iteration 4

## Capability implemented

Implemented a connected-app installation manifest compiler for ByteDesk Omnigent.

New surface:

- `POST /v1/connected-app-manifests/compile`
- Pure compiler module: `bytedesk_omnigent.connected_app_manifests`
- Route module: `bytedesk_omnigent.routes.connected_app_manifests`

The compiler turns a provider/workspace request into a deterministic integration setup manifest containing:

- Provider auth model (`oauth2`, `github_app`, or `oauth1`)
- Required OAuth/app scopes, with writeback scopes only when requested
- Webhook events to subscribe
- Omnigent ingress path and provider-specific ingress source
- Secret environment variable name for the existing ingress secret resolver
- OAuth callback URI for ByteDesk Platform/Office to mount later
- Default Omnigent task source/capability routing metadata
- Approval gates for autonomous execution and third-party writeback
- Stable idempotency-key template for webhook/event retries
- ByteDesk mount hints for Platform UI and connector setup flows

Seeded providers:

- Slack
- GitHub
- Linear
- Notion
- Google Workspace
- Microsoft Teams
- Trello

## Prior loop awareness

Before choosing this capability, I inspected open PRs with loop iteration branches:

- PR #96 / iteration 1: integration capability catalog and roadmap surface
- PR #98 / iteration 2: external work-item intake
- PR #99 / iteration 3: deterministic integration workflow plan compiler

This iteration avoids duplicating those open PRs. It adds the missing install-time contract that a ByteDesk Platform/OAuth connector UI needs before webhook events can safely reach ingress, before external work items can be created, and before workflow plans can be executed.

## Implementation description

`compile_connected_app_manifest(...)` is a pure deterministic compiler. It accepts:

- `provider`
- `workspace_id`
- `public_base_url`
- optional `desired_capabilities`
- optional `tenant_id`
- `writeback_enabled`

It normalizes provider/workspace inputs, validates the public base URL, selects a provider template, and returns a stable `ConnectedAppManifest`. The manifest id is derived from provider + workspace + tenant so ByteDesk Platform can regenerate the same setup preview without creating durable state or secrets.

Security and operational choices:

- No secrets are stored or returned.
- The manifest returns only the secret environment variable name that operators or a future secret backend should populate.
- Writeback scopes are omitted unless explicitly requested.
- Every manifest includes an approval gate before autonomous execution.
- Writeback-enabled manifests add a provider-specific approval gate before mutating third-party systems of record.

The FastAPI route is thin glue over the pure compiler and follows the existing ByteDesk extension pattern: open in single-user mode, authenticated via `require_user` when an auth provider is present.

## Business case

Third-party integrations stall when each connector has to invent its own OAuth scope list, webhook target, idempotency key, task routing defaults, and safety gates. This manifest compiler gives ByteDesk Platform a deterministic setup preview for popular connected apps, making Omnigent easier to embed into Slack, GitHub, Linear, Notion, Google Workspace, Microsoft Teams, and Trello.

For Helms AI / ByteDesk, this creates a productizable bridge from customer systems into hosted autonomous agents:

1. A user chooses a provider and workspace.
2. ByteDesk Platform calls the compiler to preview required access and safety gates.
3. The user approves OAuth/app installation.
4. Provider webhooks target the compiled Omnigent ingress source.
5. Existing/future intake and workflow-planning surfaces can convert those events into agent-managed work.

That reduces bespoke connector glue, improves customer trust through explicit scope/gate previews, and moves Omnigent toward a marketplace-ready connected-app onboarding path.

## Future unlocks

- Persist tenant-scoped connected-app installations with encrypted OAuth token references.
- Register provider-specific webhook adapters for Slack, Linear, Notion, Google Workspace, Teams, and Trello wire contracts.
- Feed compiled manifests into the iteration 2 work-item intake path for idempotent task creation.
- Feed compiled manifests into the iteration 3 workflow plan compiler as install-time prerequisites.
- Add a ByteDesk Office UI that renders scopes, webhook events, ingress source, and approval gates before installation.
- Expand seeded providers to Jira, HubSpot, Salesforce, Zendesk, Intercom, Stripe, Shopify, Discord, Asana, Monday, and Airtable.

## Verification

Targeted verification was chosen because the change is limited to a pure compiler module, one route, extension registration, tests, and this documentation.

Commands run:

- `PYTHONPATH="$PWD" uv run --extra dev python -m pytest tests/extensions/test_connected_app_manifests.py -q`
- `PYTHONPATH="$PWD" uv run --extra dev python -m ruff check bytedesk_omnigent/connected_app_manifests.py bytedesk_omnigent/routes/connected_app_manifests.py bytedesk_omnigent/extension.py tests/extensions/test_connected_app_manifests.py`
- `git diff --check`
