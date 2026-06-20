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

## Cross-replica runner dispatch (peer tunnel)

When the local ``TunnelRegistry`` misses a pinned runner, ``RunnerRouter.aclient_*``
calls ``resolve_resource("runner", id)``. If the owner replica differs, dispatch
uses ``PeerTunnelTransport`` → ``GET/POST …/v1/_coord/peer/tunnel/runner/{id}/{path}``
on the peer pod (per-pod DNS via headless Service ``omnigent-server-peer``,
``OMNIGENT_PEER_URL_TEMPLATE`` default
``http://{replica_id}.omnigent-server-peer:8000``). Loop guard: if resolve
returns this replica but the local tunnel is absent, return ``runner_unavailable``
(do not forward to self).

## Consequences

- `nats-py` dependency in omnigent
- Omnigent k8s adds `omnigent-nats` with JetStream PVC
- `omnigent-server` may scale past 1 replica when NATS coordination is configured
- Platform Redis bridge (`bytedesk_omnigent/realtime/`) unchanged