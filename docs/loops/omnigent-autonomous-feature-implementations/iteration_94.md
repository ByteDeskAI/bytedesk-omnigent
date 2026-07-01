# Omnigent autonomous feature loop — iteration 94

## Capability shipped

Iteration 94 adds an integration configuration manifest compiler and read API:

- `compile_integration_configuration_manifest(slug)` in `bytedesk_omnigent.integration_configuration_manifest`
- `GET /v1/integration-capabilities/{slug}/configuration-manifest`

The manifest translates one integration capability catalog entry into deterministic, secret-value-free setup slots that ByteDesk Platform or an autonomous onboarding agent can collect before activating the connector. It emits configuration key names, labels, required/secret flags, purposes, minimum required slot count, and category-specific deployment notes.

## Prior loop awareness

Before choosing the work, I inspected open loop PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open work already covered webhook adapters, OAuth state/refresh/scope review, activation gates, handoff packages, replay/rollback/rate-limit/idempotency/retry/dead-letter plans, acceptance suites, verification matrices, value scorecards, telemetry contracts, tool contracts, coordination topologies, evidence packets, remediation playbooks, marketplace listings, prompt packs, SLOs, data boundaries, ownership, access controls, invocation contracts, onboarding questionnaires, and capability bundles.

This iteration deliberately avoids duplicating those by focusing on the missing deployment setup surface: the inert configuration contract that tells platform UI and setup agents which environment/configuration slots must exist without ever storing or exposing secret values.

## Implementation details

- Added `ConfigurationSlot`, a small immutable dataclass with `key`, `label`, `required`, `secret`, and `purpose` fields.
- Added `compile_integration_configuration_manifest(slug)`:
  - returns `None` for unknown catalog slugs;
  - derives stable environment-style keys from catalog slugs;
  - adds OAuth client id, client secret, and redirect URI slots for OAuth-backed integrations;
  - adds webhook signing secret and webhook base URL slots for external event-capable categories;
  - emits Archon-style workflow harness slots for blueprint repository, schema version, and artifact bucket;
  - keeps all secret values absent by construction.
- Added authenticated read route `GET /v1/integration-capabilities/{slug}/configuration-manifest` beside the existing catalog and verification matrix endpoints.
- Documented the new endpoint in `omnigent/server/API.md`.

## Business case

Omnigent's integration catalog tells customers what connectors are valuable; this feature makes those connectors easier to operationalize. A deterministic configuration manifest gives ByteDesk Platform a safe form/schema source for connected-app setup, gives autonomous setup agents a checklist for missing config, and helps operators distinguish secret slots from non-secret deployment knobs before any live credential is entered.

That reduces integration onboarding friction while preserving security posture: platform surfaces can request `SLACK_COMMAND_CENTER_CLIENT_SECRET` or `GOOGLE_WORKSPACE_OPERATOR_CLIENT_ID` without ever logging, returning, or guessing the actual secret value.

## Future unlocks

- Generate ByteDesk Platform connected-app forms directly from configuration manifests.
- Feed manifests into the existing readiness, verification, and activation-gate work so setup agents can block enablement until required slots exist.
- Add provider-specific overrides for GitHub App private keys, Atlassian cloud IDs, Salesforce connected-app domains, and Microsoft tenant IDs as concrete connectors graduate from catalog candidates to implementations.
- Connect manifests to secret-manager adapters so the API can report presence/absence without exposing secret material.

## Test plan

RED:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_configuration_manifest.py -q`
- Expected failure observed first: `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_configuration_manifest'`.

GREEN / regression scope:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_configuration_manifest.py -q`
  - Result: 4 passed, 1 known warning from `tests/known_failures.yaml`.
- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_configuration_manifest.py -q`
  - Result: 14 passed, 1 known warning from `tests/known_failures.yaml`.
