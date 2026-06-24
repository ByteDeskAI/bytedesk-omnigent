# Omnigent autonomous feature loop — iteration 11

## Capability shipped

Built-in Linear webhook ingress support for Omnigent's signed inbound event pipeline.

Omnigent already had a durable `/v1/ingress/{source}` path that verifies a source adapter, resolves a `(source, match_key)` webhook binding, and wakes the parked signal wait. Iteration 11 adds a first-class `linear` source adapter so an autonomous agent can subscribe to Linear issue/project lifecycle events without custom deployment glue.

## Implementation details

- Added `LinearWebhookAdapter` in `bytedesk_omnigent/ingress.py`.
- Registered it in the webhook adapter registry under source `linear`.
- Verifies Linear's HMAC-SHA256 raw-body signature from the `Linear-Signature` header using the existing constant-time HMAC helper.
- Adds payload-aware match-key routing while preserving the existing one-argument adapter ABI:
  - `{"type":"Issue","action":"update"}` maps to `Issue.update`.
  - `{"type":"Project"}` maps to `Project`.
  - Missing event fields fall back to `Linear-Event` if present, then `*`.
- Keeps existing secret resolution: deployments configure `OMNIGENT_INGRESS_SECRET_LINEAR` or install the existing secret resolver strategy.

## Why this is high-value

Linear is a core planning and execution system for software teams. By making Linear events routable through Omnigent's durable signal bus, product/engineering agents can react to the work-management system directly:

- Auto-triage newly created bugs or customer escalations.
- Wake release, QA, or documentation agents when issues move status.
- Coordinate agent swarms around project milestones without polling Linear.
- Let ByteDesk Platform surface Linear-triggered agent work as auditable Omnigent tasks and sessions.

This complements, rather than duplicates, open loop PRs for Slack, GitHub, Stripe, Teams, generic JSON ingress, connected-app manifests, and approval-plan compilation.

## Future unlocks

- OAuth-backed Linear API client tools for comment creation, issue updates, and label/team lookup.
- A Linear connected-app manifest compiled from catalog blueprints once the iteration 4/9 surfaces land.
- Deterministic Archon-style workflow harness: `Linear Issue.create -> classify -> spawn specialist -> comment plan -> update status`.
- Cross-system loops, e.g. Linear issue transitions waking Slack/Teams notifications and GitHub PR checks.
- ByteDesk Platform UI for binding `Issue.create`, `Issue.update`, and `Comment.create` to specific house or marketplace agents.

## Verification

Targeted tests added in `tests/ingress/test_ingress.py` cover:

- Linear signature acceptance/rejection.
- Payload-derived match keys.
- End-to-end delivery through `process_inbound` for a `linear` binding.

Run scope for this iteration:

```bash
PYTHONPATH="$PWD" python -m pytest tests/ingress/test_ingress.py -q
PYTHONPATH="$PWD" python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py
```
