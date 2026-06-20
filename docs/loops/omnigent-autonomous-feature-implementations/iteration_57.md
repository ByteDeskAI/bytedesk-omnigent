# Iteration 57: integration capability gap analyzer

## Capability shipped

This iteration adds a deterministic integration capability gap analyzer in `bytedesk_omnigent.integration_gap_analysis`.

The analyzer compares the canonical integration capability catalog against caller-supplied implementation evidence and open-work evidence, then returns a JSON-ready report with:

- catalog entry totals;
- implemented and open-work coverage counts;
- sorted covered catalog slugs;
- the highest-priority uncovered `next_recommended_slug`;
- priority-ordered remaining catalog gaps;
- resolved open-work signals that can be displayed in platform planning surfaces.

The capability is intentionally pure and secret-free. It does not call GitHub, inspect local git state, read credentials, or infer tenant data. Autonomous loop runners, ByteDesk Platform, or future product UIs can supply their own evidence and receive the same deterministic prioritization.

## Prior loop awareness

Before selecting this capability, I inspected open ByteDeskAI/bytedesk-omnigent PRs with head branches matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop work already covers:

- the integration capability catalog and `/v1/integration-capabilities` endpoint;
- connected-app manifests, workflow plans, task briefs, event routes, workflow harnesses, approval gates, activation gates, replay/rollback/rate-limit/dead-letter/retry/idempotency/backfill compilers, credential/OAuth helpers, event envelopes, and contract fingerprints;
- provider-specific webhook ingress adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry.

This iteration deliberately does not add another provider adapter or duplicate the recent compiler primitives. It implements the catalog future unlock from iteration 1: automated gap analysis to compare the catalog against installed/in-flight work and identify the next non-duplicative integration investment.

## Implementation details

Added:

- `bytedesk_omnigent/integration_gap_analysis.py`
  - `IntegrationImplementationSignal`: caller-provided evidence that a catalog capability is implemented or in flight.
  - `IntegrationCapabilityGapReport`: JSON-ready coverage report.
  - `analyze_integration_capability_gaps(...)`: deterministic compiler that subtracts implemented/open catalog slugs from priority-ordered catalog entries and returns the next recommended uncovered slug.

The analyzer accepts two evidence types:

1. exact catalog slugs, such as `slack-command-center` or `github-engineering-copilot`;
2. open-work titles without exact slugs, using a conservative catalog token match so loop PR titles like `feat: add Notion backfill importer for knowledge operator` can still resolve to `notion-knowledge-operator`.

Unknown evidence is ignored rather than surfaced as a false catalog hit. This keeps the report useful for autonomous planning while preventing unrelated branch names from hiding true gaps.

Added tests:

- `tests/bytedesk_omnigent/test_integration_gap_analysis.py`
  - verifies priority-ordered gap recommendations after implemented and open-work coverage;
  - verifies title-based open-work resolution without an exact slug;
  - verifies JSON-ready, secret-free report shape.

## Business case

Omnigent's autonomous feature loop is now producing many integration PRs in parallel. Without deterministic gap analysis, future loops and product planning surfaces must repeatedly scrape PR titles and mentally compare them against the catalog, which increases duplication risk and slows execution.

This capability gives Omnigent a small but high-leverage planning primitive:

- autonomous loops can pick high-value integration work that is not already implemented or open;
- ByteDesk Platform can show customers and operators a live integration roadmap gap list;
- product leadership can reason from the same catalog surface used by agents;
- future marketplace packaging can distinguish available, in-flight, and missing connectors.

## Future unlocks

1. Expose the gap report through a read-only `/v1/integration-capability-gaps` route once ByteDesk Platform has a trusted evidence source for installed/open capabilities.
2. Feed GitHub PR metadata from the managed loop supervisor into this analyzer before choosing new autonomous iterations.
3. Add explicit implementation evidence from installed routes, enabled connector manifests, and workflow templates.
4. Render a Platform dashboard that groups gaps by category, priority, and open-work status.
5. Use the gap report to drive customer-specific connector recommendations based on tenant tool stack.

## Test plan

Targeted tests run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_gap_analysis.py -q
```

Result: `3 passed, 1 warning in 0.11s`.

The warning is the repository's existing `tests/known_failures.yaml` collection warning and is not introduced by this feature.
