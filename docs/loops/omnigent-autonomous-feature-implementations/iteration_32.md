# Iteration 32: Declarative HMAC webhook adapter

## Capability shipped

Added a declarative HMAC webhook adapter seam for SaaS integrations whose webhook contract is expressible as:

- a signature header,
- an optional signature prefix such as `sha256=` or `v1=`,
- an optional event-name header,
- a fallback event/match key.

This lets Omnigent register new header-only SaaS webhook sources without creating one bespoke Python adapter class per application.

## Prior loop awareness

Before selecting this work, I inspected open loop PRs with heads matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`.

Open prior loop PRs already cover:

- iteration 1: integration capability catalog
- iteration 2: external work item intake
- iteration 3: integration workflow plan compiler
- iteration 4: connected app manifest compiler
- iterations 5-8: Slack, Stripe, GitHub, JSON payload webhook ingress surfaces
- iterations 9-22: approval, routing, binding, secret readiness, OAuth, replay, handoff, activation, authorize URL, deterministic workflow harness work
- iterations 23-31: Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, adapter manifest, and GitLab ingress adapters

To avoid duplicating any of those PRs, this iteration does not add another named app adapter. Instead it adds a small reusable adapter primitive that future catalog/manifest work can use for long-tail SaaS webhooks such as Airtable, Notion-style partner webhooks, customer systems, and internal ByteDesk Platform event producers when their contracts are simple HMAC-over-body headers.

## Implementation details

Files changed:

- `bytedesk_omnigent/ingress.py`
  - Added `DeclarativeHmacWebhookAdapter`, a `WebhookSourceAdapter` implementation backed by a dataclass configuration.
  - Added `register_declarative_hmac_webhook_adapter(...)`, a convenience registrar that installs the configured adapter into the existing per-source webhook adapter registry.
  - Reused existing constant-time HMAC primitives and case-insensitive header lookup behavior.
- `tests/ingress/test_ingress.py`
  - Added coverage for prefix-required signature verification, event header matching, default event fallback, and registry resolution by source name.

Behavior:

- If `signature_prefix` is configured, incoming signatures must include it; this prevents accepting ambiguous bare digests when a SaaS sends versioned signature headers.
- Event routing still returns a deterministic match key, using `default_event` when no event header is configured or supplied.
- The existing default GitHub adapter remains untouched for sources without bespoke/declarative registrations.

## Business case

Omnigent's mission depends on making agents react to work where customers already operate. Each new webhook source that requires custom Python slows down integration velocity and creates review burden. A declarative HMAC adapter lowers the cost of adding common SaaS and ByteDesk Platform webhook sources by turning many integrations into data/config entries rather than code changes.

This unlocks faster onboarding for customer-specific systems and marketplace agents that need to wake on third-party events but do not need a fully bespoke protocol adapter.

## Future unlocks

- Wire `register_declarative_hmac_webhook_adapter` into the connected-app manifest compiler so catalog entries can install these adapters directly.
- Add first-class catalog examples for Airtable, Notion, and customer-defined webhook sources once their exact production header contracts are validated.
- Expose declarative adapter metadata in any adapter manifest endpoint added by prior/future loop work.
- Support additional digest encodings if needed, such as base64 HMAC or timestamped signature bases, while keeping the default minimal and deterministic.

## Test plan

Targeted tests run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_declarative_hmac_adapter_supports_header_only_saas_contracts -q
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_register_declarative_hmac_adapter_resolves_for_source -q
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q
```

The full suite was not run because the change is isolated to ingress adapter behavior; the targeted ingress suite covers existing ingress delivery behavior plus the new declarative adapter path.
