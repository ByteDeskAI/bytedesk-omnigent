# Iteration 45: Deterministic Credential Rotation Plan Compiler

## Capability shipped

Iteration 45 adds a deterministic, secret-free credential rotation plan compiler for ByteDesk Omnigent connected apps.

The new `bytedesk_omnigent.credential_rotation.compile_credential_rotation_plan` function turns connected-app credential metadata into a JSON-serializable runbook that an Omnigent agent, ByteDesk Platform worker, or human operator can execute safely. It supports webhook secrets, OAuth client secrets, and API tokens across services such as Slack, Notion, GitHub, Linear, Jira, Google Workspace, HubSpot, Salesforce, Zendesk, Intercom, Stripe, Shopify, Microsoft Teams, Discord, Asana, Monday, Airtable, and ByteDesk Platform.

The plan includes:

- normalized service/environment slugs;
- stable idempotency keys for safe upsert/retry behavior;
- bounded retry attempts;
- production and OAuth-client-secret approval reasons;
- ordered prepare/install/shadow-probe/cutover/revoke/audit steps;
- rollback actions that remain safe until the old credential is revoked;
- audit labels for ByteDesk governance;
- explicit `secret_material_included: false` so plans carry vault references and version labels, not raw secrets.

## Prior loop awareness

Before choosing this capability, I inspected the open PRs in `ByteDeskAI/bytedesk-omnigent` with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- the integration capability catalog and connected-app manifest/workflow planning foundations;
- external work item intake;
- webhook ingress adapters for Slack, Stripe, GitHub routing, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Airtable, Google Workspace, CloudEvents, Monday, ServiceNow, and Salesforce;
- webhook binding, route, replay, dead-letter, rollback, activation, approval, task-brief, probe, and workflow-harness compilers;
- OAuth state, authorize URL, and refresh plan compilers;
- rate-limit planning.

Credential rotation was selected because it is adjacent to OAuth refresh and secret readiness, but does not duplicate them: refresh keeps live tokens healthy, while this compiler plans the operational act of replacing the underlying webhook secret, OAuth client secret, or API token without leaking secret values or double-firing provider changes.

## Implementation details

Files changed:

- `bytedesk_omnigent/credential_rotation.py`
  - Adds `CredentialRotationTarget` metadata dataclass.
  - Adds `compile_credential_rotation_plan` pure compiler.
  - Adds deterministic slugging, approval-reason, step, rollback, and audit helpers.
- `tests/test_credential_rotation.py`
  - Verifies deterministic output and secret-free serialization.
  - Verifies retry bounding, OAuth approval requirements, and rollback instructions.

The compiler is intentionally pure and has no network, database, or secret-manager side effects. That keeps it safe to expose later through ByteDesk Platform APIs or to embed inside autonomous agent workflows.

## Business case

Connected app credentials expire, leak, get revoked, and must be rotated for compliance. Without a deterministic rotation artifact, every integration team repeats fragile manual runbooks and risks outages or accidental secret disclosure.

This capability gives ByteDesk Omnigent a reusable planning primitive for integration lifecycle management:

- faster enterprise onboarding because security teams can see a repeatable rotation path;
- lower operational risk because old credentials are revoked only after shadow auth and cutover;
- better agent autonomy because agents receive bounded, idempotent, rollback-aware instructions;
- stronger compliance posture because rotation evidence is audit-labeled and secret-free.

## Future unlocks

- Expose the compiler through a `/v1/integration-credential-rotations/plan` route.
- Attach compiled plans to ByteDesk task records for scheduled rotations.
- Feed the plan into the workflow harness compiler from prior loop work.
- Add provider-specific shadow probes for Slack, Google Workspace, Salesforce, Stripe, GitHub, and Microsoft Teams.
- Persist audit events into the governance cockpit after rotation completion.

## Verification

TDD and targeted verification were used:

1. RED: `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_credential_rotation.py -q`
   - Failed as expected with `ModuleNotFoundError: No module named 'bytedesk_omnigent.credential_rotation'`.
2. GREEN: implemented `bytedesk_omnigent/credential_rotation.py`.
3. Targeted test: `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_credential_rotation.py -q`
   - Passed: `2 passed, 1 warning`.

Additional verification before PR:

- targeted pytest for the new tests;
- `ruff check bytedesk_omnigent/credential_rotation.py tests/test_credential_rotation.py`;
- `python -m py_compile bytedesk_omnigent/credential_rotation.py tests/test_credential_rotation.py`;
- `git diff --check`.

The full suite was not run because the change is a small pure compiler with targeted unit coverage and the repo's full pytest matrix is expensive; no server, database, network, or dependency surfaces were changed.
