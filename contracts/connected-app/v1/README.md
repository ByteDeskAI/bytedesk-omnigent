# Omnigent Connected-App Contract v1

This directory is the language-agnostic contract between Omnigent and connected
apps such as ByteDesk Office. Implementers should generate native types from the
JSON Schemas here instead of depending on Omnigent Python or Office C# models.

The v1 wire contract uses camelCase JSON. Runtime implementations can use their
native naming conventions internally; Omnigent accepts legacy snake_case aliases
at provider ingress for rolling compatibility, but schemas define camelCase.

## HTTP Roles

- Sensor: `POST {baseUrl}/goal-sensors/{name}/evaluate`
- Actuator: `POST {baseUrl}/goal-actuators/{name}/execute`
- Goal creation: `POST /v1/goals`
- Provider registration: `POST /v1/goal-providers/register`
- Inbound event: `POST /v1/inbound/events`

## Schemas

- `goal-request.schema.json`: `POST /v1/goals` request/response envelope for a
  connected app that creates scoped Omnigent goals.
- `provider-manifest.schema.json`: registration payload for connected apps.
- `sensor-evaluate.schema.json`: engine-to-app sensor query and response.
- `actuator-execute.schema.json`: engine-to-app actuator command and response.
- `inbound-event.schema.json`: canonical app-to-engine event ingress.
- `approval-decision.schema.json`: approval callback payloads.
- `tool-event.schema.json`: tool lifecycle telemetry payloads.

## Async Events

`events.asyncapi.yaml` is the v1 event catalog. It documents the connected-app
event channels for lifecycle progress, approval requests, budget-risk asks,
approval decisions, retry scheduling, cancellation, completion, failure, and tool
lifecycle telemetry. The catalog is intentionally provider-neutral: connected apps
can deliver these events over webhooks, queues, or server streams as long as the
message payloads match the schemas.

Provider actuator `riskTier` should use the semantic string enum `low`,
`medium`, or `high`. Omnigent still accepts integers `0..5` for legacy manifests
during the v1 migration.

## Versioning

The version string is `connected-app.v1`. Fields may be added when they are
optional or have a default. Required field changes, enum removals, or semantic
changes require a new version directory. Deprecated optional fields remain
accepted for at least one minor rollout window and should be documented here
before removal.

## Correlation

Outcome events may include `normalized.goalId` directly. If the connected app only
knows its local subject id, send `normalized.subjectRef`; Omnigent resolves the
pair `(source, subjectRef)` through its goal-correlation store before booking
realized value.

Every event should include `eventId`, `traceId`, or `correlationId` when the
caller has them. `idempotencyKey` is required for canonical inbound events and
must be stable across retries for the same upstream fact.

## Ordering And Replay

Consumers must tolerate duplicate and out-of-order messages. A connected app that
replays events should preserve the original `occurredAt`, `eventId`, and
`idempotencyKey`; Omnigent stores the receive time separately. Later events for a
goal do not imply that earlier progress/tool events were delivered.

## Errors

Synchronous HTTP validation failures use the normal Omnigent API error envelope.
Asynchronous failure events use `failureClass`, `retryable`, and `detail`.
Provider-side transient errors should set `retryable: true`; authorization,
schema, and missing-scope failures should set `retryable: false`.
