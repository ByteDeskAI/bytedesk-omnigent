# ADR: Goal administration & real-time (BDP-2581)

Status: Proposed
Date: 2026-06-26

## Context

The Goal Engine (`adr-omnigent-goal-engine`) is omnigent-native and
omnigent-controlled. For it to be operable and customizable by a customer, the
goal system must be **fully administerable** — CRUD over REST with **real-time
changes** — and **customizable without forking**.

Today (`routes/goals.py`): `GET/POST /v1/goals`, `GET/PATCH /v1/goals/{id}`,
activate, dependency add/update, planner sessions, and an SSE stream
`GET /v1/goals/events` exist. Every mutation already calls
`_publish_goal_event` (`goals.py`) which fans out two ways: in-process
`event_hub` (SSE) + Redis `office:goals:{tenant}` via `realtime/bridge.py`
(consumed by the platform's SignalR `office:goals` topic). The config control
plane (ADR-0150, `routes/config.py`) already offers descriptor-driven,
hot-reloadable, per-key config with a `config.changed` SSE.

Gaps: no `DELETE`; conditions are embedded in dependency rows (no first-class
CRUD); no budget/template CRUD; the change-event stream is hand-coded per entity
(goals only); config is global (no per-tenant goal-engine knobs); there is no
provider/strategy registry seam for goals; the governance gate is admin-or-owner
only.

## Decision

### Customize-don't-fork — three layers

| Layer | Customer changes | Mechanism |
|---|---|---|
| **Configure** | tick cadence, ROI weights, default budgets/caps, autonomy posture, approval thresholds, paper-trading toggles, circuit-breaker limits | per-tenant descriptors on the **config control plane** (ADR-0150), hot-reload |
| **Extend** | new sensors/actuators/outcome-sources/webhook-listeners; custom `Optimizer`/`Treasury`/`AssignmentPolicy`/`ConditionEvaluator` | register a plugin behind a **Protocol** in a registry (`omnigent/kernel/pluggable/registry.py` `PluggableRegistry`), never fork |
| **Author** | goals, conditions, budgets, dependency trees, templates | **CRUD REST + real-time** (below) |

Principle: **we ship the engine + sensible default policies; the customer
customizes by Configure / Extend / Author only.** Every policy is a Strategy
behind a Protocol with a default impl; the core stays ours and upgradeable.

### Full CRUD admin surface (extends `routes/goals.py`)

```
/v1/goals                  GET(list,filter) POST PATCH DELETE      (DELETE: new)
/v1/goals/{id}             GET PATCH DELETE   + /activate /pause /claim /advance
/v1/goals/{id}/conditions  CRUD               (the condition AST, first-class)
/v1/goals/{id}/budget      GET PATCH          (caps, inherited view)
/v1/goal-templates         CRUD               (reusable goal+condition+cadence playbooks)
/v1/goal-providers         GET PATCH          (registered capabilities, enable/disable)
/v1/goal-config            GET PATCH          (the Configure knobs, per-tenant → config plane)
/v1/goals/outcomes         GET                (realized-value ledger, read)
/v1/goals/decisions        GET                (fund/spawn replay log, read)
```

All mutations idempotent + advisory-locked (ADR-0009), governed by the gate
below, capability-scoped (admin to mutate).

### Real-time: one canonical change stream, two transports

Generalize `_publish_goal_event` + `realtime/channel.py` from goals-only to a
typed `GoalChangeEvent{entity, op, id, tenant, payload}` covering **all**
entities (goals, conditions, budgets, templates, config, outcomes, decisions):

```
 engine/admin mutation → emit GoalChangeEvent (Redis-backed, tenant-scoped)
   ├── omnigent-native: SSE (today) / WS (optional) → ap-web admin (live, no refetch)
   └── Redis bridge → platform SignalR (office:goals) → Office projection
```

One event contract, transport per consumer; tenant-scoped (the realtime topic
discipline applies — channels keyed by dashed tenant GUID).

### Governance gate

Keep the existing admin-or-owner gate (`_require_admin`, `advance_goal_owned`,
BDP-2285). Add an optional capability check (`office.goals.administer`) and a
draft→publish path for plans (re-planning enters the same approval ceremony in
full-auto). Autonomous mode means "don't prompt between steps", not "skip
governance".

### Multi-tenancy

Per-tenant: goal tree, `goal-config`, registered providers/strategies, attribute
schema, and isolation in the store + the realtime stream. The engine is one; the
configuration, content, and capability set are per-tenant.

## Consequences

- The ap-web `/goals` admin page (`GoalsPage.tsx`, `useGoals.ts`) and the Office
  cockpit both update live as *anything* changes, not just goal status.
- Customers tailor the engine via config + registered plugins + CRUD'd content —
  the base engine stays ours and upgradeable (a product, not a bespoke fork).
- Most of this is *extend* not *build*: the config plane, the change-event fan-out,
  and the registry pattern already exist.

## Phasing

Delivered under epic BDP-2581: Phase 6 admin CRUD + real-time completion +
projection fixes (BDP-2588); Phase 7 customization + multi-tenant + arm full-auto
(BDP-2589). Engine core = `adr-omnigent-goal-engine`; extension seam =
`adr-omnigent-goal-provider-contract`.
