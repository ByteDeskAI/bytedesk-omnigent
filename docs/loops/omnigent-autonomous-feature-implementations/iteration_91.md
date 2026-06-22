# Autonomous feature loop iteration 91 — integration onboarding questionnaire compiler

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_91`

## Capability shipped

Iteration 91 adds a deterministic integration onboarding questionnaire compiler for the canonical integration capability catalog.

New API surface:

- `GET /v1/integration-capabilities/{slug}/onboarding-questionnaire`

New internal compiler:

- `bytedesk_omnigent.integration_onboarding_questionnaire.compile_integration_onboarding_questionnaire(slug)`

Given a catalog slug such as `slack-command-center`, `notion-knowledge-operator`, or `archon-style-workflow-blueprints`, the compiler returns a JSON-ready, credential-free questionnaire with:

- capability identity, category, auth model, required scopes, and risk tier;
- whether the rollout requires external provider authorization;
- shared onboarding prompts for tenant/workspace intent and pilot ownership;
- external-auth prompts only when provider scopes are required;
- activation-policy prompts tied to internal harness, external read, or external write risk;
- one category-specific section for communication, project-management, knowledge, developer, CRM/support, commerce/billing, or workflow-harness rollouts;
- `minimum_answer_count` so Platform and autonomous planning loops can score completion deterministically.

## Prior loop awareness

Before selecting this capability, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- integration invocation contracts, access-control plans, agent prompt packs, SLO profiles, deprecation plans, ownership matrices, data-boundary manifests, evidence assessment previews, remediation playbooks, coordination topologies, tool contract compiler, telemetry contracts, value scorecards, redaction profiles, acceptance suites, pilot plan compiler, gap analysis, tenant-routing manifests, evidence packets, recommendation compiler, incident drills, autonomy policies, verification assessments, consent manifests, sandbox fixtures, cutover checklists, risk registers, dependency graphs, readiness assessments, demo scenarios, staffing plans, marketplace listings, backfill plans, contract fingerprints, retry schedules, idempotency keys, event envelopes, OAuth scope review, agent blueprint previews, credential/OAuth/activation/approval/replay/rollback/rate-limit/dead-letter helpers, task briefs, handoff packages, workflow harnesses, event routes, binding APIs, connected-app manifests, and provider webhook ingress adapters.
- provider-specific webhook ingress adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry.

This iteration deliberately does not add another provider adapter and does not duplicate readiness/evidence/activation artifacts. It adds the missing pre-authorization product primitive: the questions ByteDesk Platform or an autonomous implementation loop must answer before starting OAuth, webhook binding, or workflow-harness activation.

## Implementation details

Added:

- `bytedesk_omnigent/integration_onboarding_questionnaire.py`
  - `OnboardingQuestionSection`: immutable section model with JSON-ready serialization.
  - Shared workspace-intent prompts for every catalog capability.
  - Conditional auth-boundary prompts only for capabilities with provider scopes.
  - Activation-policy prompts tied to derived risk tier.
  - Category-specific prompt sections for all current catalog categories.
  - `compile_integration_onboarding_questionnaire(slug)`, which returns `None` for unknown catalog slugs and otherwise returns a deterministic dict.

Updated:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `GET /integration-capabilities/{slug}/onboarding-questionnaire` under the existing authenticated/read-only catalog router.
  - Unknown slugs return the same `not_found` error shape used by the existing catalog detail and verification-matrix endpoints.

Added tests:

- `tests/bytedesk_omnigent/test_integration_onboarding_questionnaire.py`
  - verifies Archon-style internal workflow harness questionnaires avoid external auth and include workflow-harness rollout prompts;
  - verifies Slack questionnaires include required scopes, external-write activation policy prompts, and communication rollout questions;
  - verifies unknown slug behavior;
  - verifies the HTTP route for Notion and 404 behavior.

## Business case

Omnigent's integration catalog and many follow-on compilers help decide what to build and how to verify it. Customer activation still needs a product-facing intake step before an OAuth flow, webhook binding, or internal workflow harness is started.

The onboarding questionnaire gives ByteDesk Platform and autonomous operators a reusable intake contract:

- sales/customer-success teams can collect exactly the information needed to launch a connector pilot without asking for raw credentials;
- Platform can render a deterministic pre-flight checklist per catalog capability;
- autonomous implementation loops can convert unanswered sections into tasks for humans or specialist agents;
- external-write integrations can surface approval/rollback ownership before any provider-side mutation is possible;
- Archon-style workflow harnesses get the same structured onboarding path as third-party OAuth connectors.

## Future unlocks

1. Persist questionnaire answers per tenant/installation and feed them into activation gates.
2. Combine questionnaire completeness with verification matrices and readiness assessments for a single rollout progress view.
3. Generate ByteDesk Platform forms directly from the section schema.
4. Let autonomous agents open follow-up Tasks for unanswered sections before connector activation.
5. Add a compatibility endpoint that compares questionnaire answers against integration SLO, data-boundary, and access-control plans.

## Test plan

TDD red run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_onboarding_questionnaire.py -q
```

Initial result: expected collection failure because `bytedesk_omnigent.integration_onboarding_questionnaire` did not exist yet.

Targeted green run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_onboarding_questionnaire.py -q
```

Result: `4 passed, 1 warning in 0.15s`.

Additional regression scope:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_capabilities.py tests/bytedesk_omnigent/test_integration_verification_matrix.py tests/bytedesk_omnigent/test_integration_onboarding_questionnaire.py -q
```

Final result: `14 passed, 1 warning in 0.20s`.

Targeted lint:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/integration_onboarding_questionnaire.py bytedesk_omnigent/routes/integration_capabilities.py tests/bytedesk_omnigent/test_integration_onboarding_questionnaire.py
```

Initial result found two E501 line-length issues; both were fixed.

Final result: `All checks passed!`.

Whitespace check:

```bash
git diff --check
```

Final result: passed with no output.

The pytest warning is the repository's existing `tests/known_failures.yaml` collection warning and is not introduced by this feature.
