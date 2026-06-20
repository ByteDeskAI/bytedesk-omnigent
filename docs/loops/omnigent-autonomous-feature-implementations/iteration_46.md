# Iteration 46: capability-aware task claims

## Capability shipped

Adds a deterministic capability-aware task claim primitive to the durable Omnigent task store:

- `TaskStore.claim_task_for_capabilities(...)`
- `SqlAlchemyTaskStore.claim_task_for_capabilities(...)`

The method atomically claims an `open` task only when the requesting agent satisfies the task's `required_capability`. Tasks with no requirement remain claimable by any agent.

## Prior loop awareness

Before choosing this feature, I inspected open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open loop work already covers integration catalogs, connected-app manifests, webhook adapters for Slack/GitHub/Stripe/Teams/Linear/Shopify/Trello/Zendesk/Asana/HubSpot/Jira/Intercom/GitLab/Airtable/Google Workspace/Monday/ServiceNow/Salesforce, HMAC/JSON/CloudEvents adapters, OAuth/state/refresh planning, activation gates, replay/rollback/rate-limit/dead-letter/credential-rotation compilers, and deterministic workflow/probe/task brief harnesses.

This iteration intentionally avoids duplicating those PRs. It builds on the underlying task coordination substrate already present on `origin/develop`: `required_capability` existed on durable tasks, but task claiming did not yet provide a guard that lets schedulers or external integration intake keep specialized work in the backlog until an appropriately capable agent claims it.

## Implementation details

- Extended the abstract `TaskStore` contract with `claim_task_for_capabilities`.
- Implemented a single guarded SQL `UPDATE` in `SqlAlchemyTaskStore`:
  - `id` must match.
  - `status` must still be `open`.
  - `required_capability` must be `NULL`/blank, or exactly match one of the normalized requester capabilities.
- Preserved existing `claim_task(...)` behavior for legacy callers.
- Added regression coverage proving:
  - a mismatched agent cannot remove a capability-gated task from the open backlog,
  - a matching specialist can claim the task exactly once,
  - unrestricted tasks remain claimable with an empty capability set.

## Business case

Omnigent's marketplace and ByteDesk Platform integrations need safe routing for work arriving from external systems. A Salesforce escalation, Stripe dispute, GitHub incident, or Jira blocker should not be claimed by a generic agent just because it is first in the queue. This primitive gives schedulers, routers, and third-party integration intake a deterministic ownership gate that maps external work to agents with declared capabilities.

That improves trust, reduces misroutes, and creates a clear path to monetizable specialist agents: capability declarations become enforceable at the work-claim boundary rather than just descriptive metadata.

## Future unlocks

- Wire `/v1/tasks/claim` or the scheduler loop to call `claim_task_for_capabilities` using each agent's persisted capability surface.
- Combine with integration capability catalogs so connected-app work can declare canonical capability slugs.
- Add observability for rejected claims to surface supply gaps: which capabilities have demand but no available agent.
- Extend matching to support capability aliases or hierarchical capabilities once the canonical catalog is merged.

## Test plan

Targeted verification runs from the managed iteration 46 worktree:

```bash
PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/tasks/test_task_store.py::test_claim_task_for_capabilities_gates_required_capability tests/tasks/test_task_store.py::test_claim_task_for_capabilities_allows_unrestricted_task -q
PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/tasks/test_task_store.py -q
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/tasks/store.py tests/tasks/test_task_store.py
git diff --check
```

Results:

- RED check before implementation: the new test failed with `AttributeError: 'SqlAlchemyTaskStore' object has no attribute 'claim_task_for_capabilities'`.
- New targeted tests: `2 passed, 1 warning`.
- Full task-store test file: `5 passed, 1 warning`.
- Ruff: `All checks passed!`.
- `git diff --check`: no whitespace errors.

The warning is the repository's existing `tests/known_failures.yaml` collection warning and is unrelated to this change.
