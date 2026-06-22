# Autonomous feature loop iteration 92 — integration capability bundles

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_92`

## Capability delivered

Iteration 92 adds deterministic integration capability bundles: productized groups
of catalog integrations that describe complete agent workforce offers instead of
single connector blueprints.

New API surfaces:

- `GET /v1/integration-capabilities/bundles`
- `GET /v1/integration-capabilities/bundles/{slug}`

The bundled offers shipped in this iteration are:

1. `engineering-autonomy-stack` — GitHub + Linear/Jira + Slack for autonomous
   engineering copilots.
2. `customer-success-command-center` — Zendesk/Intercom + HubSpot/Salesforce +
   Notion for support and customer-success coordination.
3. `revenue-ops-agent-pack` — Stripe/Shopify + HubSpot/Salesforce + Google
   Workspace for revenue operations agents.

Each bundle resolves the existing integration capability catalog entries and
adds:

- target agent persona
- implementation description
- business case
- future unlocks
- product priority score
- aggregate score from resolved catalog capabilities
- deterministic activation sequence for enabling the pack safely

## Prior loop awareness

Before selecting the iteration 92 capability, I inspected open loop PRs with
heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.
The open queue already includes individual adapters, OAuth/scope planning,
activation gates, gap analysis, verification matrices, onboarding questionnaires,
marketplace listings, SLOs, ownership matrices, invocation contracts, and other
single-capability rollout artifacts through iteration 91.

This iteration intentionally avoids adding another single-service adapter or
another readiness/checklist variant. It builds on the catalog and verification
work by packaging existing catalog entries into buyer-facing agent workforce
bundles that ByteDesk Platform can display or planning agents can select as a
higher-level implementation target.

## Implementation description

- Added `bytedesk_omnigent.integration_capability_bundles` with typed dataclasses
  for:
  - `ActivationPhase`
  - `IntegrationCapabilityBundle`
  - `CompiledIntegrationCapabilityBundle`
- Added deterministic bundle definitions for engineering, customer success, and
  revenue operations.
- Resolved bundle capability slugs through the existing
  `bytedesk_omnigent.integration_capabilities` catalog, so responses reuse the
  canonical catalog metadata instead of duplicating connector descriptions.
- Added a shared activation sequence:
  1. catalog confirmation
  2. auth scope review
  3. sandbox dry run
  4. pilot with approvals
  5. production enable
- Exposed list/detail bundle endpoints from the existing integration
  capabilities router, keeping the route order ahead of the generic
  `/integration-capabilities/{slug}` route.
- Added targeted unit/API tests covering bundle ordering, compiled payload shape,
  resolved catalog entries, activation sequence, list/detail routes, and 404s.

## Business case

Customers rarely buy a raw connector in isolation. They buy an outcome: an
engineering copilot that can repair PRs, a support agent that can triage tickets,
or a revenue-ops agent that can protect renewals and payment flows. Capability
bundles turn Omnigent's integration catalog into those outcome-oriented offers.

This directly advances Omnigent's mission as autonomous agent management and
coordination middleware because it gives ByteDesk Platform a deterministic way to
present, provision, and prioritize agent workforces that span multiple third-party
applications.

## Future unlocks

1. ByteDesk Platform marketplace cards backed by bundle payloads.
2. Tenant-specific enablement flows that instantiate a whole bundle at once.
3. Pricing/packaging experiments around agent workforce bundles instead of
   individual connectors.
4. Autonomous planning loops that choose a bundle, then decompose it into the
   existing catalog capabilities, verification matrices, onboarding
   questionnaires, and activation gates.
5. Bundle-level SLOs, ownership, and evidence dashboards.

## Test plan

Targeted tests run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capability_bundles.py -q
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capability_bundles.py tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_gap_analysis.py -q
```

A full suite was not run because this change is surgical and isolated to the
ByteDesk integration capability catalog/router surface; the targeted suite covers
the new module, new API routes, and adjacent existing catalog APIs.
