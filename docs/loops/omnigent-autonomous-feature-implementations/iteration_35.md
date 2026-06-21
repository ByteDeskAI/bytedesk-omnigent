# Iteration 35: deterministic integration task brief compiler

## Capability shipped

Added a small, pure compiler in `omnigent/integration_task_brief.py` that turns normalized third-party service events into deterministic, agent-ready task briefs.

The capability is intentionally provider-SDK-free and I/O-free so it can be called from webhook ingress, OAuth callback orchestration, replay tooling, scheduler jobs, or ByteDesk Platform integration code without pulling in Slack/GitHub/Notion/etc. dependencies.

## Prior loop awareness

Before choosing this feature, I inspected open loop PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Existing open work already covers the integration capability catalog, connected app manifests, workflow/approval/replay/handoff/rollback compilers, OAuth state/authorize helpers, webhook binding/event routing, and many provider-specific webhook adapters including Slack, GitHub, Stripe, Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, plus generic JSON/HMAC adapter surfaces.

Iteration 35 does not add another provider adapter. Instead, it adds the deterministic normalization layer that a provider adapter or ByteDesk Platform ingress can use after it receives an event: compile the event into a compact task brief that an Omnigent agent can execute.

## Implementation details

New module:

- `IntegrationEvent`: provider-neutral dataclass for normalized service events.
- `compile_task_brief(...)`: returns a versioned JSON-serializable brief with:
  - source metadata: provider key, event type, resource id, optional deep link.
  - task metadata: title, objective, context, requester, routing labels, compact payload facts.
  - handoff metadata: recommended agent capabilities and deterministic next steps.
- `compile_task_brief_markdown(...)`: stable Markdown rendering suitable for spawned-agent prompts, logs, PR comments, or ByteDesk Platform work-item descriptions.

The compiler validates core routing fields and keeps payload facts deliberately compact. It includes scalar values, lists of scalar values, and one-level scalar nested facts, while avoiding full nested webhook bodies that can bloat prompts or leak irrelevant data.

## Business case

Third-party integration value depends on converting external events into useful agent work. Provider adapters can verify and parse events, but Omnigent still needs a consistent handoff contract so agents know what to do next.

This feature helps ByteDesk/Omnigent integrations by:

- reducing bespoke prompt construction across service adapters,
- making incoming events deterministic and replayable,
- giving spawned agents provider/event routing labels for assignment and analytics,
- keeping prompts compact enough for production automation,
- enabling ByteDesk Platform to turn external SaaS activity into managed Omnigent tasks.

## Future unlocks

- Wire provider-specific webhook adapters to call `compile_task_brief` before spawning or routing an agent.
- Add optional dedupe keys and idempotency metadata for webhook retry handling.
- Expose the compiled brief through a ByteDesk Platform work-item creation endpoint.
- Add provider-specific objective templates for popular events such as GitHub PR review, Slack app mentions, Notion page updates, Zendesk tickets, and Linear/Jira issue changes.
- Persist compiled briefs as replayable artifacts for deterministic Archon-style workflow harness runs.

## Test plan

Targeted tests run from the iteration 35 worktree:

- RED: `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_task_brief.py -q`
  - Failed as expected before implementation with `ModuleNotFoundError: No module named 'omnigent.integration_task_brief'`.
- GREEN: `PYTHONPATH=$PWD /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/test_integration_task_brief.py -q`
  - Passed: `7 passed, 1 warning in 0.04s`.

Full-suite pytest was not run because this is a surgical pure-Python addition with isolated targeted coverage; the targeted suite validates the new compiler's success path, Markdown rendering, validation, payload compaction, and provider capability recommendations.
