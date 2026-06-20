# Iteration 50: Integration Agent Blueprint Previews

## Capability shipped

This iteration adds a read-only Integration Agent Blueprint Preview capability to Omnigent Server:

- `GET /v1/integration-agent-blueprints`
  - Lists ranked third-party service targets with auth model, recommended scopes, trigger events, primary actions, business value, and priority.
- `GET /v1/integration-agent-blueprints/{service_slug}`
  - Returns a deterministic agent-creation payload for a specific service, including suggested agent name, harness, instructions, declared capabilities, starter tools, governance defaults, and a launch checklist.

The initial target set covers Slack, Notion, GitHub, Linear, Jira, Google Workspace, HubSpot, and Zendesk.

## Prior loop awareness

Before selecting this feature, I inspected all currently open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior work already covers:

- Integration capability catalog and roadmap (`iteration_1`).
- External work item intake, workflow plan/route/handoff/replay/approval/rollback/rate-limit/dead-letter/OAuth/secret readiness compilers.
- Webhook ingress adapters for Slack, Stripe, GitHub, JSON payloads, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry.
- Capability-aware task claims.

To avoid duplicating those PRs, iteration 50 does not add another webhook adapter, OAuth compiler, or generic integration catalog. Instead, it adds the missing bridge from service integration intent to agent creation: a deterministic agent blueprint that ByteDesk Platform or future autonomous loop agents can use to pre-fill a governed connected-app agent.

## Implementation details

Files changed:

- `omnigent/server/integration_agent_blueprints.py`
  - Pure deterministic service table and blueprint compiler.
  - Does not read secrets, call provider APIs, or mutate stores.
  - Produces JSON-ready summaries and service-specific agent creation payloads.
- `omnigent/server/routes/integration_agent_blueprints.py`
  - FastAPI router for list/detail endpoints.
  - Unknown slugs fail closed with a 404 and supported slug list.
- `omnigent/server/app.py`
  - Mounts the router under `/v1` before the SPA fallback.
- `tests/server/routes/test_integration_agent_blueprints.py`
  - TDD coverage for list, detail, and unknown-slug behavior.

## Business case

Omnigent's marketplace and ByteDesk Platform integration surfaces need to move users from “connect Slack/Notion/GitHub/etc.” to “create the right autonomous agent for this connection” with minimal manual configuration. This feature gives the platform a stable deterministic contract for that bridge:

- Product UI can show ranked integration targets and pre-fill agent creation forms.
- Autonomous loop agents can select a supported service and generate a governed starter agent without guessing scopes, instructions, or escalation defaults.
- Marketplace packaging can turn each blueprint into a reusable connected-app agent template.
- ByteDesk Platform can align connected-app authorization, inbound events, and Omnigent task queues around the same service slug.

## Future unlocks

- Add a POST endpoint that accepts workspace/service/context and returns a fully expanded draft agent manifest.
- Join these blueprints with the open integration capability catalog once that PR lands.
- Surface the blueprint list in ap-web and ByteDesk Platform agent creation flows.
- Add per-service policy templates for approval gates and write-action restrictions.
- Convert launch checklists into deterministic activation gates once integration credential state is available.
- Add marketplace packaging metadata such as pricing tier, default queue, and recommended agent persona.

## Test plan

TDD red phase:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/server/routes/test_integration_agent_blueprints.py -q`
  - Failed as expected with 404s before the route existed.

Verification run:

- `PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/server/routes/test_integration_agent_blueprints.py -q`
  - Passed: 3 tests.

Additional verification performed before PR:

- Ruff check on the new/modified Python files.
- `git diff --check`.
