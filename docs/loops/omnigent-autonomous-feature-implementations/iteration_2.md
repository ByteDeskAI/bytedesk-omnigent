# Autonomous feature loop iteration 2 — external work-item intake

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_2`

## Capability delivered

Iteration 2 adds a deterministic external work-item intake bridge that converts
third-party tracker payloads into first-class Omnigent Tasks:

- `POST /v1/tasks/intake?source=github`
- `POST /v1/tasks/intake?source=linear`
- `POST /v1/tasks/intake?source=jira`
- `POST /v1/tasks/intake?source=trello`
- generic fallback for future OAuth/service integrations

This intentionally builds on the direction of the open iteration 1 integration
capability catalog without duplicating it. Iteration 1 proposes Linear/Jira work
intake, Trello task bridge, GitHub engineering copilot, and Archon-style
deterministic workflow blueprints. Iteration 2 implements the shared substrate
those connectors need: a safe, idempotent, provider-normalizing path into the
Omnigent backlog.

## Why this is high value

Omnigent's mission is autonomous agent creation, management, coordination, and
integration into third-party applications. External systems such as GitHub,
Linear, Jira, and Trello are where real work is created, prioritized, discussed,
and completed. Without a deterministic intake seam, every connector has to invent
its own payload mapping and idempotency behavior before agents can act.

This feature lets connected apps hand Omnigent a work item and receive a durable
Task that can be claimed, assigned, routed by capability, advanced through the
existing lifecycle, and exposed through the existing task backlog API.

## Implementation description

- Added `bytedesk_omnigent.work_item_intake`:
  - normalizes GitHub issue/PR payloads;
  - normalizes Linear issue payloads;
  - normalizes Jira issue payloads;
  - normalizes Trello card payloads;
  - supports a generic provider fallback for future integrations;
  - maps provider metadata to Task title, priority, source,
    `required_capability`, labels, URL, body, and raw payload;
  - enforces idempotency using `provider + external_id` stored in Task payloads.
- Extended the existing ByteDesk Tasks router with `POST /v1/tasks/intake`.
- Kept the bridge deterministic and credential-free:
  - no secrets touched;
  - no outbound network calls;
  - no OAuth token storage;
  - no database migration;
  - no dependency on the still-open iteration 1 PR.
- Preserved existing auth behavior: multi-user mode uses the shared
  `require_user` helper; single-user/local mode stays open like sibling routes.
- Converted task API status serialization to the existing lifecycle wire value so
  new and old task responses remain JSON-safe.

## Future unlocks

1. Wire signed `/v1/ingress/{source}` webhook events directly to
   `ingest_work_item` for GitHub/Linear/Jira/Trello event handlers.
2. Attach capability-aware routing so `developer.work_item` and
   `project_management.work_item` tasks are auto-assigned to specialist agents.
3. Add provider-specific OAuth installation records and source/tenant scoping.
4. Add external write-back adapters so Task status changes post comments or move
   cards/issues in the source system.
5. Compile Archon-style deterministic workflow blueprints into a sequence of
   external work-item intake, tool-step execution, approval gates, and outcome
   write-back.
6. Use the iteration 1 catalog, once merged, to expose which catalog entries are
   backed by live intake support.

## Business case

This moves Omnigent from "agents can run" toward "agents can accept work from the
tools customers already use." It shortens connector development for high-demand
systems like GitHub, Linear, Jira, and Trello, which are common in engineering,
operations, and SMB workflows. Idempotent intake also makes webhook retries safe,
reducing duplicate autonomous work and improving customer trust.

For ByteDesk Platform, this creates a simple integration contract: connected apps
can submit work items and let Omnigent handle durable task creation, routing,
agent execution, and lifecycle tracking.

## Verification

Targeted tests added for:

- GitHub payload normalization.
- Linear task creation and replay idempotency.
- FastAPI `POST /v1/tasks/intake` create/existing behavior for Jira payloads.
- Invalid payload rejection.

Verification commands used in this iteration:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/work_item_intake.py bytedesk_omnigent/tasks/router.py tests/bytedesk_omnigent/test_work_item_intake.py
git diff --check
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_work_item_intake.py -q
```

The pytest command was run with the canonical checkout virtualenv because the
managed iteration worktree does not contain its own `.venv` directory.
