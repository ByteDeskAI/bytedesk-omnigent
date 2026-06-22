# Iteration 52: Deterministic integration event envelopes

## Capability shipped

Added a deterministic `omnigent.integration_event.v1` envelope builder for connected-app webhook ingress events.

The new `bytedesk_omnigent.integration_event_envelope` module gives agents and workflow harnesses a stable, provider-neutral event payload shape:

- `schema`: versioned envelope contract (`omnigent.integration_event.v1`)
- `source`: normalized connected-app source slug
- `event`: adapter-derived match key/event name
- `received_at`: deterministic ingress timestamp supplied by the caller
- `payload`: original provider payload, preserved as structured JSON
- `metadata`: sanitized non-secret correlation fields such as content type, delivery id, and hook id

Secrets and high-risk request headers stay at the ingress boundary. The envelope intentionally excludes authorization, cookie, token, and signature header values.

## Prior loop awareness

Before choosing this capability, I inspected the open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior work already covers source-specific webhook adapters, OAuth/credential lifecycle helpers, task briefs, route/replay/rollback/rate-limit/dead-letter plans, and adapters for Slack, Stripe, GitHub, Microsoft Teams, Linear, Shopify, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, GitLab, Google Workspace, Airtable, CloudEvents, Monday, ServiceNow, Salesforce, Notion, Bitbucket, and Sentry.

This iteration deliberately does not add another provider adapter or duplicate those plan compilers. It adds the cross-provider envelope primitive those adapters and future workflow harnesses can share.

## Implementation details

Files changed:

- `bytedesk_omnigent/integration_event_envelope.py`
  - Adds `IntegrationEventEnvelope`, a frozen dataclass for the normalized event contract.
  - Adds `build_integration_event_envelope(...)`, a pure deterministic builder.
  - Adds safe, case-insensitive header extraction for correlation metadata.
  - Normalizes connected-app source names into slugs (`Microsoft Teams` -> `microsoft-teams`).
  - Preserves empty/non-dict provider payloads as `{}` instead of leaking ambiguous `None` into downstream agent context.
- `tests/ingress/test_event_envelope.py`
  - Covers source/event/payload preservation.
  - Covers source slugging and empty-payload defaults.
  - Verifies sensitive signature/authorization headers are not echoed into the envelope payload.

## Business case

Connected apps are only valuable if an Omnigent agent can understand what happened without bespoke provider glue on every turn. A normalized ingress event envelope makes every webhook-capable integration easier to mount into ByteDesk Platform, Office workflows, and autonomous agents:

- Agents get consistent context across GitHub, Slack, Linear, Teams, Jira, Notion, and future apps.
- Workflow harnesses can route on `source` + `event` deterministically.
- Support/debug tooling can correlate events without exposing credentials or signatures.
- The platform can store/replay provider events using a versioned schema instead of ad hoc raw payloads.

## Future unlocks

- Add an opt-in ingress route mode that delivers this envelope to the signal bus instead of raw provider payloads.
- Extend webhook binding records with a `payload_mode` field (`raw` vs `integration_event.v1`) for backward-compatible rollout.
- Use envelopes as the input contract for deterministic Archon-style workflow harness nodes.
- Add provider-specific metadata extractors for Slack request IDs, Stripe event IDs, Jira webhook IDs, and Notion delivery correlation once those adapters are merged.
- Surface envelope schema in `/v1/integration-capabilities` when the catalog endpoint lands.

## Verification

TDD cycle used:

1. Added `tests/ingress/test_event_envelope.py` first.
2. Ran the targeted test and confirmed the expected red failure: `ModuleNotFoundError: No module named 'bytedesk_omnigent.integration_event_envelope'`.
3. Implemented the minimal module.
4. Re-ran targeted tests with the existing ingress suite.

Commands run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_event_envelope.py -q
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_event_envelope.py tests/ingress/test_ingress.py -q
```

Result:

- `tests/ingress/test_event_envelope.py`: 2 passed
- `tests/ingress/test_event_envelope.py tests/ingress/test_ingress.py`: 9 passed

Full suite was not run because this is a surgical pure-helper addition with targeted ingress coverage.
