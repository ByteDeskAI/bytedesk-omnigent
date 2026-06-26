# ADR: Runner self-heal + host auto-failover (BDP-2579)

Status: Proposed
Date: 2026-06-26

## Context

A session is pinned to a runner (`conversations.runner_id`) on a host
(`conversations.host_id`). The runner process subscribes its own NATS subject
(`omnigent.runtime.runner.<runner_id>.http`) directly (`omnigent/runner/_entry.py`
→ `serve_runner_nats`). When the runner dies — most commonly a host
roll/restart on deploy (`workflow.mjs land` rolls the source-mounted
`omnigent-host` StatefulSet) — the subject loses its subscriber and every
runner-backed call fails with NATS `NoRespondersError`.

On session open / `switchTo`, the web UI eagerly fires `GET .../stream` +
`GET .../resources/*`. Today those **read paths** hit the dead runner → **503**
→ the client retry-storms (`"stream open failed (503), will retry"`). The host
is still live and *can* relaunch (`sessions.py:1790` — "host can relaunch, just
send a message"), but the read paths neither relaunch nor degrade — they loop.

This is **not** the multi-replica gap (BDP-2571/2572): the runner subject is
`runner_id`-keyed/global, so both `omnigent-server` replicas behave identically.
`NoRespondersError` = dead runner, not an unreachable replica.

A second failure exists beyond a dead runner on a *live* host: the **host
itself** may be wedged or gone (BDP-2491 wedged-host, BDP-2540 deregister). A
relaunch on that host will never succeed; the session must move to another host.

## Decision

Make runner-backed read paths self-healing via a bounded **escalation ladder**,
guarded by a single cross-replica claim per session so concurrent reads (and
both replicas) coalesce onto one repair:

```
read detects dead runner  (NoRespondersError / runner_is_online()==False)
   │  acquire single-flight claim_resource("session-heal", session_id)   [ADR-0009 single-writer]
   ▼
[Rung 1] RELAUNCH on the bound host
   │  up to host.relaunch.maxAttempts, each ≤ host.relaunch.attemptTimeout, backoff
   │  wedged-host = BDP-2491 acked=False → counts as a failed attempt
   ▼ exhausted (host offline/wedged, or runner never comes online)
[Rung 2] FAIL OVER to a new host                                          [NEW]
   │  evict + cooldown the bad host (circuit-breaker)
   │  select a live, capability-matching host (Strategy), excluding failed+cooldown
   │  relaunch the runner there
   │  ATOMIC repin conversations.{host_id,runner_id} via compare-and-swap
   │  up to failover.maxHops
   ▼ all hosts exhausted / hops spent
[Rung 3] GRACEFUL "offline / unrecoverable"  (never a 503 storm)
```

While any rung is in flight, reads **hold-then-serve** up to
`runner.reconnect.holdTimeout` (≈5–8s) then return a benign `reconnecting`
state — the UI shows "reconnecting", never an error loop.

### Invariants (correctness-critical)

1. **Single-writer.** All rungs for a session run under one
   `claim_resource("session-heal", session_id)` (coordination backplane). Two
   replicas / N concurrent reads never launch two runners or fail over to two
   hosts. Losers await the winner's result.
2. **Atomic repin.** Rung 1 CAS `runner_id` (`WHERE runner_id=:dead`); Rung 2 CAS
   `(host_id, runner_id)` together (`WHERE host_id=:dead AND runner_id=:dead`).
   The two columns never split across a hop.
3. **Liveness-gated.** Relaunch only when the runner is genuinely dead AND
   (rung 1) the host is live. Never relaunch a healthy runner; never onto a dead
   host.
4. **Bounded + terminal fallback.** Every rung has a hard attempt/time/hop cap;
   exhaustion falls to Rung 3 (graceful offline), never an unbounded loop.
5. **Idempotent launch.** Reuse the existing launch ack/pending machinery
   (`_launch_runner_on_host` / `_launch_runner_on_host_id`, BDP-2491
   `acked=False` eviction). A retried launch never strands a half-spawned runner.

### Host selection (new, Strategy — ADR-0008)

`host_id` is client-provided at session create (`body.host_id`), so there is no
server-side selector to reuse for failover. Introduce a minimal
`HostSelector` Protocol with a default `LiveHostSelector` implementation:
choose from `host_store` a host where `host_is_live(host)`, excluding the failed
host and any in cooldown; deterministic tiebreak (least-recently-failed /
stable order) for now, load-aware later. Pluggable so org-shared-pool
(ADR-0151) policies can swap it.

### Circuit-breaker (new)

On failover, `HostRegistry.evict` the failed host (BDP-2491) and record a
cooldown (`failover.hostCooldown`); the selector skips cooled-down hosts.
Half-open after cooldown so a transiently-wedged host rejoins. Prevents
flapping back onto the broken host.

### Continuity caveat

A new host = fresh sandbox (per-pod emptyDir). Conversation history is in the DB
and survives; the runner working-dir/sandbox state does not. Plain chat sessions
re-hydrate from DB/bundle — correct. **Managed/sandbox sessions** (dev-project
work with a working tree) must fail over through the managed-host relaunch path
(`relaunch_managed_host`, `sessions.py:5532`), not the plain relaunch, so a new
sandbox generation is provisioned. Failover branches on managed-vs-plain.

### Configuration (IOptions-style; env + Helm mirror)

| Key | Default | Meaning |
|---|---|---|
| `runner.reconnect.holdTimeout` | 8s | read hold-then-degrade cap |
| `host.relaunch.maxAttempts` | 3 | rung-1 relaunch tries on bound host |
| `host.relaunch.attemptTimeout` | 10s | per relaunch attempt |
| `failover.enabled` | **true** | rung-2 host failover on by default (flag retained for kill-switch) |
| `failover.maxHops` | 2 | distinct hosts tried before rung 3 |
| `failover.hostCooldown` | 60s | bad-host circuit-breaker cooldown |

`failover.enabled` ships **true** (user decision); the flag remains a kill-switch
if failover misbehaves in prod.

### Observability

Emit a `runner.failover` stream + telemetry event (session, old→new host, rung
reached, attempts) so the UI shows "moved to a new host" and ops can alert on
failover rate (a spike = unhealthy host fleet).

## Patterns

Strategy (host selection) · Circuit Breaker (bad-host cooldown) ·
Retry-with-backoff escalating to failover · Single-writer/advisory-lock
(ADR-0009) · atomic CAS repin (saga-ish) · graceful degradation.

## Scope

In: the heal/failover ladder on the read paths (`/stream`, `/resources`), the
single-flight claim, CAS repin (`conversation_store`), `HostSelector`,
host circuit-breaker, config + observability, on-by-default. Reuses existing
launch + managed-relaunch machinery.

Out: changing the multi-replica runner addressing (already global); the BDP-2491/
2540/2571/2572 fixes (done); load-aware host scheduling (selector is pluggable
for later).

## Revisions from architect review (BINDING — supersede the above where they conflict)

- **F1 — real lock, not `claim_resource`.** `claim_resource` is last-write-wins
  presence (`coordination/nats_backplane.py:121`), not a mutex, and its KV bucket
  has no TTL. Add `CoordinationBackplane.try_acquire(kind, id, *, ttl_s) -> bool`
  backed by JetStream `kv.create()` (atomic create-only) in a **dedicated locks
  bucket created with `KeyValueConfig(ttl=...)`** so a crashed holder self-expires;
  release via `kv.delete`. No new `ResourceKind` literal — use a lock-name string.
  Losers don't need a result channel: on failed acquire, re-read
  `conversation.runner_id` and `_wait_for_runner_client` (the winner's repin is the
  observable result). Single-replica/test backplane returns `True` (no-op lock).
- **F2 — detector signal.** `NoRespondersError` is never surfaced raw. Unary
  `/resources`: the NATS transport wraps every error into `httpx.ConnectError`
  (`transport.py:53`). Stream `/stream`: `_handle_stream_request` (`transport.py:108`)
  re-raises raw nats errors that the relay's `except` misses. **Fix in the
  transport**: wrap `_handle_stream_request` errors into `httpx.ConnectError` too,
  so both read paths fail uniformly. Detector catches `httpx.ConnectError` +
  `OmnigentError(code=RUNNER_UNAVAILABLE)` (resolve-time, `routing.py:285`).
- **F3 — atomic CAS + stop pre-stamping.** `set_runner_id` is NULL→value only;
  `replace_runner_id`/`set_host_id` are last-write-wins in separate txns; launch
  helpers (`sessions.py:5335/5443`) pre-stamp via `replace_runner_id`. Add
  `cas_runner_id(id, expected_dead, new)` and `cas_host_and_runner(id, exp_host,
  exp_runner, new_host, new_runner)` (single UPDATE each), and route the heal path
  through them instead of `replace_runner_id`. Respect
  `ck_conversations_workspace_required_for_host` (keep `workspace` non-null) and
  the `host_id` FK (target host row must exist).
- **F4 — managed sessions are relaunch-only; selector is tenancy/capability-aware.**
  Discriminator = `host.sandbox_provider is not None` (load `Host` by
  `conv.host_id`); there is no conversation-level managed flag. Managed → relaunch
  a new sandbox generation via the managed path (in `omnigent/server/managed_hosts.py`,
  NOT `sessions.py:5532`) — **never** cross-host failover (fresh sandbox = work-tree
  loss). Plain → `HostSelector` scoped to `list_hosts(conv.created_by)` (or an
  explicit ADR-0151 org-pool policy), filtered by `host_is_live`,
  `sandbox_provider is None`, and harness-capability (`configured_harnesses` ∋ the
  session harness), excluding the failed host + cooldown.
- **F5 — breaker trips only on host-wedge, cooldown in shared index.** Evict
  (`host_registry.py:295`) only on the BDP-2491 `acked=False` host-wedge signal
  (`sessions.py:5390/5460`), never on a runner-only failure (a healthy host can run
  a crash-looping runner). `evict` is loop-affine + disrupts all co-tenant sessions
  on the host, and only works on the owning replica → for a host owned by another
  replica, publish an evict request over the backplane. Cooldown lives in the
  coordination index with an explicit expiry epoch (bucket has no key TTL).
- **F6 — reuse existing client status shapes.** No new `reconnecting`/
  `runner.failover` wire events. Reuse `session.terminal_pending`
  (`SessionTerminalPendingEvent`, `sessions.py:4868`) for the spinning-up state and
  `_publish_runner_recovered_status` (`4841`) on recovery — the UI already renders
  these.
- **F7 — build/ship order (still one PR).** Implement bottom-up: (1) transport wrap
  (F2) + unified detector; (2) real lock (F1); (3) CAS store methods + heal-path
  refactor off `replace_runner_id` (F3); (4) rung-1 relaunch + rung-3 graceful via
  reused status shapes; (5) rung-2 selector/breaker (F4/F5) behind `failover.enabled`.
  Rungs 1+3 alone fix the reported deploy-roll symptom; rung-2 covers a host that
  does not return. **Managed default = relaunch-only (no cross-host), regardless of
  `failover.enabled`**; `failover.enabled=true` governs cross-host failover for
  PLAIN sessions only.

## Consequences

- Deploys/host rolls no longer strand open sessions; they self-heal or fail over.
- New distributed-correctness surface (the claim + CAS) — covered by a 2-replica
  + killed-runner + killed-host integration test.
- On-by-default failover changes prod behavior day one; `failover.enabled=false`
  is the kill-switch.
