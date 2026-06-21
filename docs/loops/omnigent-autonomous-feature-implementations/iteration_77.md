# Omnigent autonomous feature loop iteration 77

## Capability shipped

Iteration 77 adds deterministic integration redaction profiles for the canonical integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/redaction-profile`

New compiler surface:

- `bytedesk_omnigent.integration_redaction_profile.compile_integration_redaction_profile(slug)`

The compiler turns a catalog integration such as `slack-command-center`, `github-engineering-copilot`, or `archon-style-workflow-blueprints` into a secret-free logging and evidence-retention contract. It tells autonomous agents, ByteDesk Platform, and future workflow harnesses which headers, request bodies, provider payload fields, and harness outputs should be allowed, summarized, hashed, or redacted before an integration is activated for a tenant.

## Prior loop awareness

Before selecting this capability, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- the integration catalog and `/v1/integration-capabilities` endpoint;
- provider webhook ingress adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry;
- connected app manifests, OAuth helpers, workflow plans/harnesses, approval and activation gates, replay/rollback/rate-limit/dead-letter/retry/idempotency/backfill/credential-rotation artifacts;
- readiness, risk, dependency, cutover, sandbox, consent, verification, recommendation, evidence, pilot-plan, acceptance-suite, autonomy-policy, and gap-analysis surfaces.

This iteration deliberately does not add another provider adapter or duplicate verification/acceptance/autonomy outputs. It adds the missing privacy and observability primitive that those surfaces need before they can safely retain integration evidence: a deterministic redaction profile per catalog capability.

The canonical checkout had unrelated WIP, so the managed workflow operator initially refused to create the worktree. I reran the same managed operator with `--allow-dirty`, leaving the canonical WIP untouched.

## Implementation details

Added:

- `bytedesk_omnigent/integration_redaction_profile.py`
  - `RedactionFieldRule`: immutable field-level logging instruction model.
  - `compile_integration_redaction_profile(slug)`: returns `None` for unknown slugs and otherwise emits a JSON-ready profile.
  - Shared base rules for authorization headers, cookies, webhook signatures, and provider object IDs.
  - Extra outbound mutation redaction for `external_write` integrations.
  - Category-specific rules for communication, project management, knowledge, developer, CRM/support, commerce/billing, and workflow-harness capabilities.
  - Risk-aware default log levels and retention policies.
  - Sensitive-scope extraction so Platform UI can flag high-risk OAuth scopes without storing credentials.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `GET /integration-capabilities/{slug}/redaction-profile` under the existing authenticated/local-mode catalog router.
  - Unknown catalog slugs return the same `not_found` shape as neighboring catalog routes.

Added tests:

- `tests/bytedesk_omnigent/test_integration_redaction_profile.py`
  - verifies external-write Slack profiles redact write payloads and summarize communication content;
  - verifies internal workflow harness profiles keep structured evidence while hashing phase outputs;
  - verifies unknown slug behavior;
  - verifies the HTTP route for GitHub engineering copilot and 404 behavior.

## Business case

Omnigent's integration roadmap now has many connectors and deterministic readiness artifacts. The next enterprise trust blocker is evidence safety: agents and operators need enough logs to prove what happened, but they must not leak OAuth tokens, webhook signatures, customer conversations, repository diffs, payment details, CRM timelines, or generated customer artifacts.

Redaction profiles make integration observability safe by default:

- ByteDesk Platform can display clear logging/retention expectations during connector setup;
- autonomous integration agents can attach a profile before executing provider probes or acceptance suites;
- enterprise reviewers can see that Omnigent separates traceability metadata from raw sensitive payloads;
- future audit-ledger storage can reject evidence that violates the profile before persistence;
- workflow-harness outputs inspired by Archon can preserve deterministic replay hashes without retaining private generated artifacts.

This directly advances Omnigent's mission as agent middleware: safe third-party application integration, coordinated autonomous execution, and platform-ready trust controls.

## Future unlocks

1. Persist redaction profiles per tenant integration installation and version them alongside OAuth scope grants.
2. Enforce profiles in webhook ingress, acceptance-suite execution, verification evidence assessment, and outcome-ledger writes.
3. Let ByteDesk Platform render profile-driven warnings such as `metadata_only` logging or zero-day raw payload retention.
4. Add a redaction-profile assessment endpoint that checks submitted evidence for disallowed raw fields before activation.
5. Generate provider-specific redaction fixtures for Slack, GitHub, Google Workspace, CRM/support, commerce, and workflow-harness connectors.

## Test plan

Targeted TDD and verification were run from the managed iteration 77 worktree using the canonical virtualenv and `PYTHONPATH=$PWD`.

RED:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_redaction_profile.py -q
```

Result: failed as expected during collection with `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_redaction_profile'`.

GREEN targeted suite:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_redaction_profile.py -q
```

Result: `4 passed, 1 warning in 0.15s`.

Broader related regression scope:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_redaction_profile.py tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py -q
```

Result: `14 passed, 1 warning in 0.17s`.

Targeted lint:

```bash
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/ruff check bytedesk_omnigent/integration_redaction_profile.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_redaction_profile.py
```

Result: `All checks passed!`.

Full-suite pytest was intentionally skipped because this is a surgical, read-only compiler/API addition. The targeted suite covers the new compiler, route exposure, and neighboring catalog/verification-matrix behavior. The pytest warning is the repository's existing `tests/known_failures.yaml` collection warning and is not introduced by this feature.
