# ADR: Omnigent pluggable coordination backplane (NATS)

**Status:** Accepted (2026-06-20)  
**Scope:** `bytedesk-omnigent` only — runtime, deploy, tests. No ByteDesk Platform service changes.

## Context

`omnigent-server` is pinned to `replicas: 1` because runner/host tunnel registries, pending
elicitation indexes, and SSE fan-out are process-local. A server pod restart or a second replica
loses routing state and splits pending-approval visibility.

Postgres already covers durable workflow signals (`SqlAlchemySignalBus`), cron claims, and
relational SoT. The gap is **ephemeral cross-replica coordination** with **crash-survivable indexes**.

## Decision

Introduce a pluggable **`CoordinationBackplane`** seam (`omnigent/coordination/`) with:

| Implementation | Use |
|----------------|-----|
| `inprocess` | Default — zero deps, single-replica / tests |
| `nats` | Production HA — JetStream KV + bounded JetStream stream |

Activate `nats` when `OMNIGENT_NATS_URL` is set (override via `OMNIGENT_USE_COORDINATION_BACKPLANE`).

### NATS assets (omnigent namespace)

| Asset | Name | Purpose |
|-------|------|---------|
| KV | `omnigent-coord-registry` | `runner.{id}` / `host.{id}` → `replica_id` |
| KV | `omnigent-pending-index` | Durable pending elicitation/input index |
| Stream | `OMNIGENT_COORD_EVENTS` | Cross-replica coordination events (limits retention) |
| Subject | `omnigent.coord.fanout.>` | Ephemeral fan-out |

Subjects use dashed GUIDs where ids are GUIDs (BDP-1397). Prefix `omnigent.` — not platform `wf.*`.

### Idempotency

Stream delivery is at-least-once; consumers dedupe on `(conversation_id, event_id)` or
`elicitation_id`. Aligns with Idempotent Receiver (ADR-0009).

## Non-goals

- Replacing Postgres signal bus, cron scheduler, or conversation stores
- Replacing live WebSocket termination (backplane routes *to* owner replica)
- Platform `ByteDesk.Realtime` / RabbitMQ / gateway changes
- NATS broker clustering in MVP (JetStream PVC fixes process-state loss; broker HA is phase 4)
- Sharing the same NATS store_dir/PVC for coordination and agent artifacts

## Cross-replica runner dispatch

Superseded by the runtime flags / NATS control-plane ADR. When the local
runner registry misses a pinned runner, ``RunnerRouter.aclient_*`` may still
call ``resolve_resource("runner", id)`` to distinguish a missing runner from
stale coordination ownership. It no longer forwards HTTP through a peer
WebSocket tunnel. Runner HTTP dispatch uses the configured runner transport
factory and the default factory is NATS request/reply. If a runner has
coordination ownership but no local launch-token record, dispatch returns
``runner_unavailable`` so the caller can rebind the session instead of
silently falling back to legacy WebSocket forwarding.

## Consequences

- `nats-py` dependency in omnigent
- Omnigent k8s adds `omnigent-nats` with JetStream PVC for coordination
- Agent artifacts use a separate `omnigent-nats-artifacts` JetStream Object Store
  instance/PVC so bundle growth cannot exhaust coordination KV/streams
- `omnigent-server` may scale past 1 replica when NATS coordination is configured
- Platform Redis bridge (`bytedesk_omnigent/realtime/`) unchanged
