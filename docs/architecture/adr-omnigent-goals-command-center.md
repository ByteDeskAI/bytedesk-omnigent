# ADR: Goals Command Center — AI-assistant goal cockpit (BDP-2592)

Status: Proposed
Date: 2026-06-27

## Context

Phase 1 (BDP-2581) built the Goal Engine; Phase 2 (BDP-2592) makes it fully
autonomous. The founder needs a single surface to **set, monitor, and observe**
the autonomous org — not a passive dashboard but an **AI assistant you converse
with** that can drive the whole engine, alongside live views of what it's doing.

The seams already exist in omnigent `ap-web`: `MainAgentSurface`
(`pages/ChatPage.tsx`) + `chatStore.send/switchTo` (embeddable agent chat), the
`PlannerPanel` one-shot planning-session pattern (`pages/GoalsPage.tsx` →
`POST /v1/goals/planner/sessions`), the goal MCP tools (`tools/goal_tools.py`),
the `/v1/goals/events` SSE + `SessionUpdatesProvider`/`RunnerHealthProvider` for
live activity, and the Mission Control tokens. Nothing ties them into a cockpit.

## Decision

A new omnigent `ap-web` route — the **Goals Command Center** — is the primary
surface for the goal system. It pairs a **conversational goal-commander agent**
with **live observability + autonomy controls**, all token-driven.

```
┌──────────────────────── Goals Command Center ────────────────────────┐
│  CONVERSE (set)            │  OBSERVE (monitor)                        │
│  goal-commander agent      │  • ROI frontier — what's running + why    │
│  (MainAgentSurface, a      │  • activity feed — spawned sessions /     │
│   persistent session)      │    outcomes (SSE + SessionUpdates)        │
│  drives the engine via     │  • treasury / budget burn                 │
│  the full toolset ↓        │  • decision-replay ledger + waiting-reasons│
│                            │  • AUTONOMY: posture · KILL SWITCH · caps │
└────────────────────────────┴───────────────────────────────────────────┘
```

### Goal-commander agent + toolset
A chief-of-staff-class agent (provisioned via `scripts/bytedesk/apply_goal_planner.py`
or a sibling) holds the **full** engine toolset — existing
create/list/claim/advance/dependency + the Phase-1 admin CRUD + **new** tools:
`goal_prioritize`, `goal_adjust_budget`, `goal_set_posture` (arm/disarm),
`goal_read_frontier`, `goal_read_decisions`, `goal_read_ledger`,
`goal_batch_approve`, `goal_decompose`. So "create a goal to grow MRR, give it
$5k, and arm it" is a conversation, not a form.

### Chat = persistent commander session
Reuse `MainAgentSurface` bound (via `chatStore.switchTo`) to a **persistent**
commander session per scope (the PlannerPanel pattern, upgraded from one-shot).

### Live cockpit = new token-driven components
ROI frontier, activity feed, treasury burn, decision-replay, waiting-reasons,
and the **autonomy posture control + kill switch + spend caps** — new components
under `components/ui/.../command-center/`, Mission Control tokens only, no
framer-motion, must survive the embed scope rewrite.

### Invariants
1. The command center is a **client of the engine**, never a second source of
   truth — every mutation goes through the goal tools / REST, governed + audited.
2. The **kill switch** sets posture to `gated` (or pauses dispatch) immediately;
   it must be reachable in one click and never itself gated.
3. Read views subscribe to the existing tenant-scoped realtime; no new global feed.

## Consequences
- The autonomous org becomes legible + steerable from one surface; the human is
  the board (direction + budget + the switch), the agent is the operator.
- `/goals` stays the admin overlay; platform `/office/goals` stays the founder
  read/govern projection (optional revenue-rollup widget).

## Phasing
Delivered in Wave 5 (BDP-2598); the arm switch goes live in Wave 6 (BDP-2599)
once the Wave-1 end-to-end proof + safety pass.
