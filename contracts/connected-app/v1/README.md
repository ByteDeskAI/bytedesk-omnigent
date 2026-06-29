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
- Provider registration: `POST /v1/goal-providers/register`
- Inbound event: `POST /v1/inbound/events`

## Async Events

`events.asyncapi.yaml` is the v1 event catalog. It documents the connected-app
event channels for lifecycle progress, approval requests, budget-risk asks,
completion, and failure. The catalog is intentionally provider-neutral: connected
apps can deliver these events over webhooks, queues, or server streams as long as
the message payloads match the schemas.

Provider actuator `riskTier` should use the semantic string enum `low`,
`medium`, or `high`. Omnigent still accepts integers `0..5` for legacy manifests
during the v1 migration.

## Correlation

Outcome events may include `normalized.goalId` directly. If the connected app only
knows its local subject id, send `normalized.subjectRef`; Omnigent resolves the
pair `(source, subjectRef)` through its goal-correlation store before booking
realized value.
