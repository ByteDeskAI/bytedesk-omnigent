# Iteration 68: Integration verification evidence assessment

## Capability shipped

Iteration 68 adds a deterministic, secret-free verification evidence assessment capability for integration rollout gates.

New API surface:

- `POST /v1/integration-capabilities/{slug}/verification-assessment`

New compiler surface:

- `bytedesk_omnigent.integration_verification_assessment.assess_integration_verification_evidence(...)`

The endpoint accepts a `provided_evidence` object keyed by verification gate id and compares submitted evidence labels against the existing integration verification matrix. The response marks each gate `complete` or `incomplete`, returns missing evidence labels, and summarizes overall rollout status.

## Prior loop awareness

Before choosing this feature, I inspected all currently open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open loop work already covers adapter manifests, provider ingress adapters, OAuth helpers, activation gates, replay/rollback/rate-limit/retry/idempotency/backfill/rotation/approval/readiness/demo/staffing/marketplace/risk/dependency/cutover/sandbox/consent artifacts, and the recently added integration verification matrix.

This iteration deliberately builds on the verification matrix instead of duplicating those artifacts: it turns a static matrix into an actionable assessment result that platform UI, autonomous loops, and operators can use to decide whether an integration is ready to activate.

## Implementation details

- Added `bytedesk_omnigent/integration_verification_assessment.py`.
  - Resolves a capability through the existing verification matrix compiler.
  - Accepts secret-free evidence labels grouped by gate id.
  - Normalizes evidence strings by trimming blanks and de-duplicating repeated labels.
  - Counts only required evidence labels, ignoring unrelated caller input.
  - Returns a JSON-ready assessment with per-gate completion and missing evidence.
- Extended `bytedesk_omnigent/routes/integration_capabilities.py`.
  - Adds `POST /integration-capabilities/{slug}/verification-assessment` under the existing router.
  - Preserves existing `require_user(...)` auth behavior.
  - Returns 404 for unknown catalog slugs.
  - Returns deterministic 422 responses for malformed evidence payloads instead of leaking 500s.
- Added `tests/bytedesk_omnigent/test_integration_verification_assessment.py`.
  - Verifies incomplete and complete assessments.
  - Verifies unknown slug handling.
  - Verifies route exposure and malformed payload validation.

## Business case

Enterprise and SMB customers will not trust autonomous third-party integrations unless Omnigent can show exactly why a connector is safe to activate. The verification matrix names the gates; this iteration makes those gates operational by computing a clear status from operator-provided evidence.

That directly supports ByteDesk Platform integration because the UI can now show a checklist-style readiness state for Slack, GitHub, Google Workspace, Linear/Jira, Notion, CRM/support, commerce, or workflow-harness connectors without requiring live credentials or exposing secrets.

## Future unlocks

- Persist evidence assessments per tenant/workspace as activation records.
- Let autonomous integration setup agents attach evidence as they complete OAuth, webhook, idempotency, policy, observability, and rollback tasks.
- Gate production activation on `status == "complete"` for high-risk integrations.
- Render ByteDesk Platform progress bars and missing-evidence callouts from the endpoint response.
- Convert missing evidence directly into Omnigent Tasks assigned to integration setup agents.

## Test plan

Commands run from the managed iteration worktree:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_verification_assessment.py::test_integration_capability_route_rejects_malformed_evidence_payloads -q
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_verification_assessment.py -q
```

The first command verifies the regression found during review: malformed payloads now return 422 instead of 500. The second command verifies the full new assessment test file.

A broader targeted integration-catalog test run and `git diff --check` were also run before opening the PR; see the PR conversation for exact output.
