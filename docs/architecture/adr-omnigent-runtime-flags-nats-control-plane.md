# ADR: Omnigent runtime flags and NATS runner control plane

**Status:** Accepted (2026-06-26)  
**Scope:** `bytedesk-omnigent` runtime, ByteDesk extension package, tests. No ByteDesk Platform service changes in this ADR.

## Context

Omnigent needs a self-contained, LaunchDarkly-like runtime control surface for
turning capabilities on and off without redeploying. The initial use case is
replacing the Omnigent runner WebSocket dispatch substrate with a NATS-backed
control plane, then managing that rollout through runtime flags.

The relevant LaunchDarkly capability shape is:

- typed flag variations, including boolean and multivariate flags
- evaluation contexts and custom targeting rules
- individual targeting, prerequisites, percentage rollouts, and default rules
- persistent flag data that can serve last-known state locally
- explicit evaluation/test endpoints for administration and preview

## Decision

Introduce `bytedesk_omnigent.runtime_flags` as an SDK-authored ByteDesk
extension. New feature-flag functionality must enter through the public
`omnigent.sdk` facade:

- `@extension(name="bytedesk.runtime_flags", requires=("omnigent.coordination",))`
- `@router()` for the admin/evaluation HTTP surface
- `@provides(RuntimeFlagStore)` for serving-store injection

The serving store defaults to NATS JetStream KV, using
`OMNIGENT_FLAG_DEFINITIONS` for revisioned flag definitions and
`omnigent.flags.changed` for update notifications. An explicit in-memory store
exists for tests and narrow local runs only; it is not a production fallback.

Runner server-to-runner HTTP dispatch now resolves through a pluggable transport
factory and defaults to NATS request/reply:

- server requests publish to `omnigent.runtime.runner.{runner_id}.http`
- runner processes subscribe to their subject and dispatch payloads through the
  existing runner FastAPI ASGI app
- `OMNIGENT_NATS_URL` is required for the production control plane
- no WebSocket fallback is registered in the runner transport factory
- cross-replica runner misses no longer use the peer WebSocket tunnel; stale
  coordination ownership without a launch token returns `runner_unavailable`

## API Surface

Runtime flags expose:

- `GET /v1/flags`
- `POST /v1/flags`
- `GET /v1/flags/{key}`
- `PATCH /v1/flags/{key}` with `If-Match`
- `POST /v1/flags/{key}/evaluate`
- `GET /v1/flags/{key}/history`
- `POST /v1/flags/{key}/rollback`

Flag definitions include typed variations, targets, prerequisite keys, rules,
percentage rollout buckets, lifecycle state, owner, timestamps, and revision.

## Consequences

- Feature flag administration is delivered as a ByteDesk extension rather than
  a core route edit.
- NATS becomes mandatory infrastructure for the direct replacement path.
- The runner dispatch path no longer instantiates the old runner WebSocket
  transport or peer WebSocket forwarding transport.
- The legacy runner WebSocket route, runner WebSocket transport package, and
  peer WebSocket forwarding route are removed rather than retained as fallbacks.
- `RunnerControlRegistry` keeps launch ownership/token metadata only; it does
  not expose live runner sessions and does not provide a WebSocket dispatch path.
- Host control, browser terminal attach, and local/native app-server WebSockets
  remain separate protocol surfaces. They are not the removed runner dispatch
  tunnel and are not registered as runner transport fallbacks.

## Non-goals

- A full LaunchDarkly clone in the first increment: no experimentation metrics,
  approvals workflow, segments UI, audit UI, or mobile/client SDKs yet.
- Platform `ByteDesk.Realtime` changes.
- Keeping a legacy runner WebSocket fallback.
