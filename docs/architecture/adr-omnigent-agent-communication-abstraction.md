# ADR: Agent communication abstraction

**Status:** Accepted (2026-07-01)
**Scope:** `bytedesk-omnigent` session chat, delegation, stream projection, and blueprint child dispatch. Public REST/SSE schemas and tool schemas are unchanged.

## Context

Agent chat is implemented across several mature surfaces:

- `/v1/sessions` routes create sessions, accept events, bind runners, publish SSE, and build snapshots.
- `ConversationStore` is the durable source of truth for conversations, items, parent/child links, and audit rows.
- `session_stream` is the ephemeral live transport with bounded `Last-Event-ID` replay plus snapshot fallback.
- `sys_session_send`, `sys_session_create`, blueprint child nodes, runner sub-agent bookkeeping, and ap-web all encode parent-to-child delegation in slightly different shapes.
- Native terminal sessions have a transcript-forwarder single-writer invariant: web messages are forwarded to the runner and are not AP-persisted until the native transcript round-trips.

The result works, but the design is hard to scale because admission, dispatch, delegation, lifecycle projection, and reconnect behavior are coupled inside the route module and duplicated at tool/runner/UI edges.

## Decision

Introduce `omnigent.communications` as the internal domain seam for chat and delegation:

- `commands.py` defines typed command objects for starting sessions, posting session events, and delegating to child agents.
- `results.py` defines typed command/delegation results.
- `events.py` defines internal domain events for status, accepted input, child updates, and blueprint-node progress.
- `state.py` defines the canonical session status vocabulary and status transition policy.

Public contracts stay where they are today:

- REST request/response Pydantic models remain in `omnigent.server.schemas`.
- SSE wire events remain `ServerStreamEvent` variants.
- Tool schemas for `sys_session_send`, `sys_session_create`, `sys_read_inbox`, and related builtins do not change.
- `ConversationStore` remains the durable source of truth.
- NATS/session fanout remains a coordination/live-event substrate, not the durable chat history.

The first code path moved behind this seam is the session status transition rule. `_publish_status` now asks `communications.state.should_publish_status()` whether a status edge should be emitted, preserving the existing sticky `failed -> idle` suppression while making the rule reusable for future projectors and delegation services.

## Patterns

This follows the repo's existing direction from the pluggable-core ADR:

- Use typed domain contracts for wire-adjacent behavior.
- Keep public Pydantic schemas at the API boundary.
- Introduce narrow internal seams before extracting services from high-risk route code.
- Prefer deterministic command/result/event objects over ad-hoc dictionaries between chat, tools, blueprints, runners, and UI orchestration.

## Invariants

- **Persist before forward for non-native sessions.** A snapshot immediately after POST must see the user item before runner dispatch.
- **Native terminal single writer.** AP does not persist native web composer messages; the native transcript forwarder remains the committed transcript source.
- **Failed status is sticky against trailing idle.** `failed -> idle` quiescence signals are suppressed until real work resumes or runner recovery explicitly clears the stale failure.
- **Snapshot remains the recovery source of truth.** `Last-Event-ID` replay is bounded and in-memory; clients still reconcile with `GET /v1/sessions/{id}`.
- **Parent/child session links remain durable.** Delegation services must use `Conversation.parent_conversation_id` and related store methods, not transient stream-only state.

## Migration Plan

1. Land pure communication contracts and central status transition rules.
2. Extract a `ChatApplicationService` for session create/post-event admission while preserving the route surface.
3. Extract event dispatch strategies for SDK, native terminal, blueprint executor, and server-only event types.
4. Extract a `DelegationService` used by `sys_session_send`, `sys_session_create`, blueprint child nodes, and UI child-session operations.
5. Extract a `ChatEventProjector` that converts internal domain events into SSE/status/child-summary projections.
6. Reduce ap-web compensating logic only after the server projector has stable tests.

## Non-goals

- No REST, SDK, SSE, or tool-schema migration in this increment.
- No database schema changes.
- No replacement of `ConversationStore`.
- No use of NATS as durable history.
- No behavioral change to native terminal transcript ownership.

## Verification

Each extraction step must preserve targeted tests for:

- session status transition behavior, especially sticky `failed -> idle`
- `Last-Event-ID` replay and snapshot fallback
- native terminal forward-without-AP-persist behavior
- `sys_session_send` and `sys_session_create` delegation handles
- blueprint child-node dispatch/result projection
- ap-web session bind/post/stream event handling
