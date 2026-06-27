# ADR: Goal Engine — autonomous economic loop (BDP-2581)

Status: Proposed
Date: 2026-06-26

## Context

Omnigent already has a durable goal backlog: `SqlAlchemyGoalStore`
(`bytedesk_omnigent/goals.py`), the ADR-0154 two-key delivery projector
(`goals_delivery.py`), org/dept/agent scoping + readiness/dependencies
(migration `e1f2a3b4c5d6`), an accountability loop
(`accountability/loop.py`), and a durable cron scheduler
(`scheduler/scheduler.py`, `scheduler/loop.py`).

But the backlog is **passive**. Three live pieces have never been joined:

1. **The clock** — `scheduler/scheduler.py` fires `cron_triggers` exactly-once,
   but `scheduler/loop.py` dispatch is a **log-only stub**
   (`_log_only_dispatch`: `"cron fire (no dispatch wired yet)"`).
2. **The spawn seam** — `_execute_session_create`
   (`omnigent/runner/tool_dispatch.py`) → `POST /v1/sessions` starts an agent
   turn, but nothing calls it *from a goal*.
3. **The goal store** — claim/advance/dependencies exist, but reaching
   `ready` triggers no work; the accountability loop only reopens/escalates
   state, never spawns an agent.

So goals change state only via external webhooks and humans. There is **no
autonomous loop**: nothing makes an agent *work* a goal.

This ADR makes goals the **autonomous economic engine** of the omnigent org —
the heartbeat that drives change, accomplishment, and income. The entire engine
is **omnigent-native and omnigent-controlled**; extensibility (revenue rails,
external sensors) is delivered by the connected-app Provider Contract
(`adr-omnigent-goal-provider-contract`), which feeds and acts for the engine
but never owns or decides goals.

## Decision

A **Goal is standing organizational intent with a P&L** — an expected value, a
budget (cost), and a *measured* realized value. The engine runs a periodic
**tick** that allocates finite agent-effort to the highest expected-return
goals, books realized value from real revenue rails, and reinvests. Priority is
**computed from economics, not set by hand**.

```
                         ── THE ORG TICK (advisory-locked, ~30s) ──
 list ready/recurring-due goals
   │  resolve(goal): evaluate sensor conditions → actionable? + waiting_reasons
   ▼
 frontier = actionable goals,  ranked by roi = EV·confidence / remaining_budget
   │  (per-tier budget partition, global ROI tie-break)
   ▼
 for g in frontier (top-down until budget/spawn caps exhausted):
   │   treasury.circuit_open(g)? → skip
   │   treasury.can_fund(g, est_cost)? → else skip
   │   g.risk_tier==HIGH and not paper_trading → enqueue_approval; skip
   │   res = treasury.reserve(g, est_cost)          [ADR-0009 exactly-once]
   │   session = dispatch(g, res)   ── _execute_session_create → POST /v1/sessions
   │   record_decision(g, session)                  [replay/audit ledger]
   ▼
 roll up child outcomes → parent goals (generalize _maybe_complete_goal)
```

### Goal model (extends `goals.py` / `db_models.py`)

Existing fields stay. Add:

- **Tier + cascade:** `tier` (`org|department|agent`, derived from scope depth),
  `parent_goal_id` (self-FK) — the org→dept→agent decomposition edge. Constraints
  (budget, priority, deadline, risk) inherit down; outcomes roll up.
- **Cadence** — maps to the *existing* scheduler:
  - `immediate` — dispatch on readiness; close on `done`.
  - `recurring(cron, tz)` — registers a `cron_triggers` row at create; **never
    closes**; each fire makes incremental progress toward a standing metric
    (carries a progress accumulator).
  - `until_done(heartbeat_s)` — re-spawn every heartbeat until the success
    condition trips (long push, observable + interruptible).
- **Economics:** `expected_value_cents`, `realized_value_cents` (booked by the
  ledger, never declared by an agent), `confidence` (0..1, learned),
  `success_condition` (a `ConditionRef` — "done" is *evaluated*, not declared),
  `risk_tier` (`low|medium|high`).
- **Budget:** a `GoalBudget` (token/spend/spawn caps), inherited down the tree.
- **Extensibility:** typed `attributes` JSONB + optional per-tenant schema, so
  customers add fields without migrations.

`roi` is derived, never stored: `(expected_value_cents * confidence) /
max(budget.remaining_cents, 1)`, risk/time-decayed by the optimizer.

### Conditions (the dependency generalization)

Dependencies become an open **Condition AST** evaluated by pluggable **sensors**
(full spec in `adr-omnigent-goal-provider-contract`). A goal is `actionable`
only when its `preconditions` (an `All`/`Any`/`Not` tree of
`Leaf{sensor, query, predicate}`) are satisfied. The existing dep kinds
(`milestone/epic/github_pr/jira_issue`) become built-in sensors; the two-key
delivery projector becomes the `jira` + `github` sensors. The resolver emits
`waiting_reasons` (first-class, projected to admin surfaces) so the founder
always sees *why* the org isn't doing something.

### Treasury (the reinvestment flywheel)

`engine/treasury.py` — a `Treasury` Protocol + default impl:

- **Hierarchical caps** inherited org→dept→agent→goal; a spawn checks the whole
  chain.
- `reserve`/`settle` — exactly-once spend accounting (ADR-0009 guarded UPDATE).
- `replenish(tier, booked_cents)` — a booked outcome refills its tier's budget:
  **realized revenue funds the next round** (compounding).
- `circuit_open` — global kill-switch + anomaly auto-pause (budget burned with
  zero realized value over N ticks).

New tables: `goal_outcomes` (the realized-value ledger; written only by an
OutcomeSource, never an agent) and `goal_decisions` (every fund/spawn decision
with its ROI rationale — replay/audit).

### The Dispatcher (the keystone — replaces the stub)

`dispatch(goal, reservation)` resolves the owning agent (`assignment.py`,
tier-aware: exclude `system`/`workflow` agents) and spawns via
`_execute_session_create` → `POST /v1/sessions`, rendering the goal +
success-condition + budget as the turn intent. Idempotent per
`(goal, cadence-period)` under a coordination-backplane advisory lock + a claim
row. This single function turns the static backlog into a living organization.

### Safety (built before arming full-auto)

- **Paper-trading** — a goal runs `dry_run` (simulated spend, predicted outcome)
  until its economics validate over K ticks; then it earns real budget.
- **Blast-radius gate** — any high-risk actuator (send money, email a customer,
  deploy prod) hits the approval gate even in full-auto. Autonomy governs
  *internal capital allocation*, never irreversible external acts.
- **Circuit breakers** — per-tier spend caps + global kill-switch + anomaly
  auto-pause, all in Treasury.

### Invariants (correctness-critical)

1. **Single-writer / exactly-once.** Reserve, claim, dispatch, and outcome-book
   run under advisory locks (ADR-0009); the tick holds one
   `advisory_locked_loop` lock (distinct key, no contention with accountability
   `0x61636374626C7479` or cron `0x63726F6E5F736368`).
2. **Multi-replica safe.** All spawn/claim/book run under the coordination
   backplane advisory lock — at `replicas>1` without the NATS backplane, locks
   must NOT silently no-op the engine (test-asserted; cf. the accountability
   degradation risk after the NATS cutover).
3. **Realized value is never declared by an agent** — only booked by an
   OutcomeSource from a rail that actually billed.
4. **Every policy is a Protocol with a default** (ADR-0008): `Optimizer`,
   `Treasury`, `AssignmentPolicy`, `ConditionEvaluator` — swappable per tenant,
   never forked (`adr-omnigent-goal-administration-realtime`).
5. **Standalone-first.** The engine runs the full loop with built-in
   fallbacks (in-memory provider); a connected app makes it richer + real-money
   but is not required for the core to exist or be tested.

## Consequences

- **Goals stop being records and become the org's capital-allocation engine.**
  The tick funds the highest-ROI agent-turns within budget and reinvests
  realized revenue — an autonomous business, with humans steering direction +
  budget, and Office as a read/govern projection.
- New modules live under `bytedesk_omnigent/engine/` (`loop.py`, `treasury.py`,
  `optimizer.py`, `dispatcher.py`) + a new advisory-lock key; registered in
  `extension.py` `background_tasks`. The goal store/model extend in place.
- The ADR-0142 inbound primitive + ADR-0155 pipeline gain their killer use case
  (external sensors + outcome push).
- Cost: a real autonomous loop spends tokens; the safety layer (paper-trading,
  budgets, circuit breakers) is the precondition for arming it full-auto.

## Phasing

Delivered under epic BDP-2581: Phase 1 keystone (dispatcher + cadence,
BDP-2583), Phase 2 sensor/condition system (BDP-2584), Phase 3 economics
(treasury/optimizer/ledger/safety, BDP-2585). Admin/real-time surface =
`adr-omnigent-goal-administration-realtime`; extensibility =
`adr-omnigent-goal-provider-contract`.

## Phase 2 — Full autonomy (BDP-2592)

Phase 1 shipped the engine skeleton; several decisions in this ADR were built as
seams/defaults but not fully wired (honestly: `success_condition` was stored but
never evaluated; assignment + actuator seams were inert; `confidence`/EV were
static; cost was a flat constant; the learning loop, economic rebalancer,
opportunity discovery, sub-goal decomposition, and market mechanics were
deferred). Epic **BDP-2592** completes them:

- **Close the loop** (Wave 1, BDP-2594): the tick evaluates `success_condition`
  → auto-completes a goal ("done is evaluated, not declared"); the dispatcher
  consumes the `goal_assignment` registry; actuators are consumed (agent tools +
  deterministic actuator goals); a per-model cost model feeds `est_cost` and
  `settle()` runs with real usage; the dormant config knobs are wired; and a real
  agent working a seeded goal → done → outcome booked → replenish is proven e2e.
- **Real-world reach** (Wave 2, BDP-2595): live `kpi/jira/github/http/mcp`
  sensors (native + via the registered connected-app provider); Jira two-way.
- **Agentic intelligence** (Wave 3, BDP-2596): learning loop (confidence/EV from
  the ledger), the accountability loop becomes an economic rebalancer, recurring
  progress accumulator + until_done heartbeat, auto-decomposition, dynamic
  sub-goal spawn, opportunity discovery.
- **Market mechanics** (Wave 4, BDP-2597) — see `adr-omnigent-goal-market-mechanics`.
- **Command center** (Wave 5, BDP-2598) — see `adr-omnigent-goals-command-center`.
- **Arm full_auto** (Wave 6, BDP-2599): founder org flipped to `full_auto` behind
  the safety layer + kill switch; other tenants stay `gated`.
