# Iteration 22 — Deterministic integration workflow harness compiler

## Capability shipped

Iteration 22 adds a read-only, deterministic integration workflow harness compiler for third-party application integrations:

- Pure compiler: `bytedesk_omnigent.integration_harness.compile_integration_harness`
- API route mounted through the ByteDesk extension: `GET /v1/integration-workflow-harness`
- Targeted coverage for both pure compiler behavior and route serialization

Given a provider, objective, agent id, and external object, the compiler returns an Archon-style phase contract with stable phases:

1. `intake`
2. `auth_readiness`
3. `plan`
4. `dry_run`
5. `execute`
6. `verify`
7. `handoff`

Each phase carries a gate, required evidence, retry posture, and audit event. The plan also includes provider-normalized OAuth scope defaults, webhook event defaults for popular services, and a stable idempotency key that downstream tools can reuse before mutating an external system.

## Prior loop awareness

Before selecting this capability, I inspected open Omnigent autonomous feature-loop PRs whose head branches match `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Existing open loop work already covers:

- Integration capability catalog
- External work item intake
- Integration workflow plan compiler
- Connected app manifest compiler
- Slack/Stripe/GitHub/JSON/Teams/Linear/Shopify webhook ingress adapters
- Integration approval plans
- Webhook binding management
- Integration event routes
- Secret readiness plans
- OAuth state tokens and authorize URL compiler
- Webhook ingress preflight preview
- Integration replay plans
- Integration handoff packages
- Integration activation gates

This iteration avoids duplicating those surfaces. It does not add a new webhook adapter, OAuth flow, activation gate, manifest compiler, or replay/handoff package. Instead it adds the deterministic execution harness contract that can sit above those pieces: a provider-aware, evidence-gated skeleton an agent must satisfy before and after touching the external application.

## Implementation details

### `bytedesk_omnigent.integration_harness`

New frozen dataclasses:

- `IntegrationHarnessPhase`
- `IntegrationHarnessPlan`

New public function:

- `compile_integration_harness(provider, objective, agent_id, external_object)`

Provider defaults currently cover common integration targets inspired by the requested catalog and popular SaaS surfaces:

- Slack
- Notion
- Trello
- GitHub
- Linear
- Jira
- Google Workspace
- HubSpot
- Salesforce
- Zendesk
- Intercom
- Stripe
- Shopify
- Microsoft Teams
- Discord
- Asana
- Monday
- Airtable

Unknown providers normalize safely and return an empty scope/event default set rather than inventing credentials.

### `bytedesk_omnigent.routes.integration_harness`

Adds:

- `GET /v1/integration-workflow-harness?provider=&objective=&agent_id=&external_object=`

The route returns only a deterministic JSON plan. It does not read secrets, call external APIs, or mutate state.

### Extension registration

The ByteDesk extension now includes the integration harness router in `BytedeskExtension.routers()`, so the route mounts through the existing extension seam alongside governance, ingress, goals, and tasks.

## Business case

Omnigent needs to become a safe execution layer for autonomous agents operating inside third-party applications. The most valuable enterprise integrations are not just webhook receivers or OAuth handshakes; they need repeatable, inspectable execution contracts so users and platform operators can answer:

- What exactly will the agent touch?
- Which credentials and scopes are required?
- Was there a dry-run before mutation?
- What approval unlocked execution?
- What external receipt proves the mutation happened?
- What snapshot verifies final state?
- What handoff should humans or downstream agents see?

This compiler gives ByteDesk Platform a stable contract for rendering integration setup and execution readiness before agents act in Slack, Jira, GitHub, Google Workspace, HubSpot, Salesforce, Zendesk, and other systems.

## Future unlocks

- Feed the compiled phases into the existing approval, activation, replay, and handoff surfaces when those loop PRs land.
- Persist harness phase progress as task/tool-step evidence so workflow runs become resumable and auditable.
- Expose provider defaults from the integration capability catalog once `/v1/integration-capabilities` lands on develop.
- Add platform UI cards that render each phase gate and evidence requirement for admin approval.
- Bind the idempotency key into webhook/event-route dispatch so external writes are replay-safe by default.

## Verification

Targeted tests run from the iteration 22 worktree:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_harness.py tests/routes/test_integration_harness_route.py -q
```

Result:

- `4 passed, 1 warning in 0.14s`

The warning is the repository's existing `tests/known_failures.yaml` unmatched-entry warning emitted by `tests/conftest.py`; it is unrelated to this change.
