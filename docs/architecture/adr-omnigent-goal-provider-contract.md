# ADR: Connected-App Goal Provider Contract (BDP-2581)

Status: Proposed
Date: 2026-06-26

## Context

The Goal Engine (`adr-omnigent-goal-engine`) is omnigent-native and reasons over
**opaque** capabilities — it must not know what "sales" or "Stripe" or a GitHub
webhook payload means. Revenue logic and external connectivity are
**domain knowledge** that belongs in the connected app (the ByteDesk platform,
via Office), which already has the Sales pipeline, DevProjects deploy, Stripe
billing, public HTTPS ingress, the webhook secrets, and payload knowledge.

The existing cross-repo seam is one-directional: the platform *reads* goals via
`IGovernance`/`OmnigentGovernancePort` → `GET /v1/goals` (ADR-0152). This ADR
adds the **inverse capability plane**: omnigent asks a connected app to *sense*
and *act*, and the app pushes *outcomes* and *events* back. Omnigent stays
domain-agnostic and reusable for any connected app; the platform owns its
revenue logic where it already lives.

## Decision

A connected app implements a **Goal Provider Contract** of four opaque roles.
The app may *supply truth*, *perform requested side-effects*, *report realized
value*, and *project state* — it can **never author, prioritize, budget,
schedule, or complete a goal**. All reasoning stays in omnigent.

### The four roles (Protocols, omnigent-defined)

```python
class Sensor(Protocol):        # truth (pull or push)
    name: str
    async def evaluate(query) -> Reading        # {satisfied, value, observed_at, stale_after_s}

class Actuator(Protocol):      # side-effects (request/reply)
    name: str; risk_tier: RiskTier              # high-risk always gates, even full-auto
    async def execute(action) -> Result

class OutcomeSource:           # realized $ — RECEIVED, not called: app pushes to the ledger
class WebhookListener:         # raw external webhook → CanonicalInboundEvent → push
```

`CanonicalInboundEvent = {source, type, idempotency_key, occurred_at,
subject_ref, payload}` — the ONE shape omnigent understands (ADR-0155 already
defines it).

### Call direction

```
External system (GitHub/Jira/Stripe/Calendly…)
   │ raw webhook (signed)
   ▼
CONNECTED APP = WebhookListener (owns the edge: ingress + secrets + translation)
   │ push CanonicalInboundEvent
   ▼
OMNIGENT canonical sink: POST /v1/inbound/events → ADR-0155 pipeline → sensors → engine
            ▲ evaluate (pull)            │ execute (request/reply)
            │ outcome push (realized $)  ▼
CONNECTED APP: Sensor.evaluate / Actuator.execute / OutcomeSource → ledger
```

- **Sensors** = pull (`evaluate`) or push (event via ADR-0155 inbound) — same
  condition system consumes both.
- **Actuators** = request/reply: omnigent → app `execute`. The app owns the
  side-effect + its own auth/permissions/idempotency on its rails.
- **Outcomes** = push only: the app books the dollar (it owns Stripe/Sales) and
  pushes to the omnigent ledger. **Realized value is asserted only by the rail
  that billed.**
- **Webhooks** = the app receives + validates the signature (where the secrets
  live) + translates to canonical + pushes. Fixes the current query-string-secret
  gap (Jira isn't HMAC-signed in the omnigent-hosted path).

### Registry + remote adapters (omnigent-owned, generic)

`ProviderRegistry` mirrors `omnigent/kernel/pluggable/registry.py`
(`PluggableRegistry`) and the `SecretBackend` precedent
(`omnigent/onboarding/secrets.py`). A connected app registers a **manifest**:

```
{ provider: "sales",
  sensors: ["opportunity","lead_score","pipeline_value"],
  actuators: [{name:"advance_stage",risk:"low"},{name:"send_outreach",risk:"high"}],
  outcomes: ["closed_won"],
  webhook_sources: ["github","jira","stripe"] }
```

The engine only ever sees `Sensor`/`Actuator`/`OutcomeSource`; the single
concrete impls omnigent ships are `RemoteSensorAdapter`/`RemoteActuatorAdapter`
(HTTP, forward to the registered provider URL with reverse auth). Omnigent stays
domain-blind. Registration source of truth = the config control plane (ADR-0150),
declarative + survives restarts.

### Two hosting modes (declared per source)

- **App-hosted (preferred):** the connected app hosts the receivers + sensors +
  actuators; new external source = new translator in the app, **zero omnigent
  change.**
- **Omnigent built-in (fallback):** today's `POST /v1/goal-delivery/{github|jira}`
  + a `WebhookTranslator` Protocol + an in-memory fake provider — so the engine
  + tests run **standalone** with no connected app.

### Invariants

1. A connected app can **never** author/prioritize/budget/schedule/complete a
   goal. It supplies truth, acts on request, reports value, projects state.
2. Realized value is booked only by an OutcomeSource (the billing rail), never by
   the engine or an agent.
3. The engine never learns domain meaning — a "sales" condition is just
   `Leaf(sensor="sales.opportunity", …)` routed to a remote provider.
4. Standalone-first: built-in fallbacks let omnigent run the full loop with no
   connected app.

## Consequences

- Omnigent becomes a reusable, productizable autonomous-business engine; swap the
  connected app and the engine is unchanged.
- **Lights up ADR-0155** (the inbound pipeline gets its first live caller via the
  canonical event/outcome ingress — P8).
- Enables the **Jira two-way control surface**: a human comment/@mention → app
  WebhookListener → canonical event → sensor → owning agent's turn.
- The platform implements this contract in `0156-office-goal-provider-implementation`
  (Sales first), wrapping existing Sales/DevProjects/Billing surfaces.

## Phasing

Delivered under epic BDP-2581: Phase 4 provider contract + registry + adapters +
canonical ingress (BDP-2586); Phase 5 platform implements it, Sales first
(BDP-2587). Engine = `adr-omnigent-goal-engine`; admin/real-time =
`adr-omnigent-goal-administration-realtime`.
