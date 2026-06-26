"""Canonical fabric envelopes and record models."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from typing import Any, ClassVar, TypeVar, get_args, get_origin

FABRIC_SCHEMA_VERSION = 1

T = TypeVar("T")


def _unix_ms() -> int:
    return int(time.time() * 1000)


def _schema_name(cls: type) -> str:
    explicit = getattr(cls, "schema_name", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    name = cls.__name__
    out: list[str] = []
    for idx, char in enumerate(name):
        if char.isupper() and idx > 0:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is None:
        return getattr(annotation, "__name__", str(annotation))
    args = ",".join(_type_name(arg) for arg in get_args(annotation))
    return f"{getattr(origin, '__name__', str(origin))}[{args}]"


def fabric_schema_hash(model_type: type) -> str:
    """Return a stable hash for a fabric model's canonical field shape."""
    model_fields = []
    for item in fields(model_type):
        model_fields.append(
            {
                "name": item.name,
                "type": _type_name(item.type),
                "required": item.default is MISSING and item.default_factory is MISSING,
            }
        )
    schema = {
        "schema_version": FABRIC_SCHEMA_VERSION,
        "schema_name": _schema_name(model_type),
        "fields": model_fields,
    }
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_as_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_as_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _as_jsonable(item) for key, item in value.items()}
    if is_dataclass(value):
        return value.to_dict() if hasattr(value, "to_dict") else {
            item.name: _as_jsonable(getattr(value, item.name)) for item in fields(value)
        }
    return value


def _dataclass_to_dict(instance: Any) -> dict[str, Any]:
    return {item.name: _as_jsonable(getattr(instance, item.name)) for item in fields(instance)}


def _dataclass_from_dict(model_type: type[T], data: dict[str, Any]) -> T:
    values: dict[str, Any] = {}
    for item in fields(model_type):
        if item.name not in data:
            continue
        value = data[item.name]
        annotation = item.type
        if isinstance(value, dict) and hasattr(annotation, "from_dict"):
            value = annotation.from_dict(value)
        elif get_origin(annotation) is dict and isinstance(value, dict):
            value = dict(value)
        elif get_origin(annotation) is tuple and isinstance(value, list):
            value = tuple(value)
        values[item.name] = value
    return model_type(**values)


@dataclass(frozen=True)
class CredentialReference:
    schema_name: ClassVar[str] = "credential_reference"

    ref: str
    principal_id: str
    expires_unix_ms: int
    scope: str = "runner"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CredentialReference:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class SchedulerJob:
    schema_name: ClassVar[str] = "scheduler_job"

    job_id: str
    schedule_id: str
    tenant_id: str
    org_id: str
    lane: str
    fire_at_unix_ms: int
    idempotency_key: str
    payload_ref: str
    attempt: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchedulerJob:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class RunnerJob:
    schema_name: ClassVar[str] = "runner_job"

    job_id: str
    session_id: str
    tenant_id: str
    org_id: str
    lane: str
    epoch: int
    deadline_unix_ms: int
    capacity: dict[str, Any]
    credential: CredentialReference
    idempotency_key: str | None = None
    affinity: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunnerJob:
        credential = data.get("credential")
        copied = dict(data)
        if isinstance(credential, dict):
            copied["credential"] = CredentialReference.from_dict(credential)
        if isinstance(copied.get("capabilities"), list):
            copied["capabilities"] = tuple(str(item) for item in copied["capabilities"])
        return _dataclass_from_dict(cls, copied)


@dataclass(frozen=True)
class RunnerHeartbeat:
    schema_name: ClassVar[str] = "runner_heartbeat"

    runner_id: str
    session_id: str | None
    host_id: str
    lane: str
    epoch: int
    state: str
    observed_unix_ms: int
    lease_id: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunnerHeartbeat:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class LifecycleEvent:
    schema_name: ClassVar[str] = "lifecycle_event"

    event_id: str
    runner_id: str
    session_id: str | None
    stage: str
    occurred_unix_ms: int
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LifecycleEvent:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class PlacementDecision:
    schema_name: ClassVar[str] = "placement_decision"

    decision_id: str
    runner_job_id: str
    lane: str
    mode: str
    host_id: str | None
    runner_id: str | None
    reason: str
    decided_unix_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlacementDecision:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class LeaseRecord:
    schema_name: ClassVar[str] = "lease_record"

    lease_id: str
    owner_id: str
    resource_id: str
    epoch: int
    expires_unix_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LeaseRecord:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class CapacityRecord:
    schema_name: ClassVar[str] = "capacity_record"

    scope: str
    key: str
    limit: int
    used: int
    updated_unix_ms: int
    circuit_open: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapacityRecord:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class DlqRecord:
    schema_name: ClassVar[str] = "dlq_record"

    dlq_id: str
    source_subject: str
    source_stream: str
    idempotency_key: str
    reason: str
    payload_ref: str
    failed_unix_ms: int
    deliveries: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DlqRecord:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class TimelineEvent:
    schema_name: ClassVar[str] = "timeline_event"

    event_id: str
    session_id: str
    stage: str
    message: str
    occurred_unix_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TimelineEvent:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class AuditEvent:
    schema_name: ClassVar[str] = "audit_event"

    event_id: str
    actor_id: str
    action: str
    target: str
    occurred_unix_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEvent:
        return _dataclass_from_dict(cls, data)


@dataclass(frozen=True)
class FabricEnvelope:
    """Envelope Wrapper for fabric messages crossing NATS boundaries."""

    subject: str
    idempotency_key: str
    payload_type: str
    payload: Any
    schema_version: int = FABRIC_SCHEMA_VERSION
    schema_hash: str = ""
    produced_unix_ms: int = field(default_factory=_unix_ms)
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def wrap(
        cls,
        *,
        subject: str,
        idempotency_key: str,
        payload: Any,
        produced_unix_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FabricEnvelope:
        payload_type = _schema_name(type(payload))
        return cls(
            subject=subject,
            idempotency_key=idempotency_key,
            payload_type=payload_type,
            payload=payload,
            schema_hash=fabric_schema_hash(type(payload)),
            produced_unix_ms=_unix_ms() if produced_unix_ms is None else produced_unix_ms,
            headers=dict(headers or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "schema_hash": self.schema_hash,
            "subject": self.subject,
            "idempotency_key": self.idempotency_key,
            "payload_type": self.payload_type,
            "produced_unix_ms": self.produced_unix_ms,
            "headers": dict(self.headers),
            "payload": _as_jsonable(self.payload),
        }

    def to_json(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str, payload_type: type[T]) -> FabricEnvelope:
        decoded = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        payload_data = decoded.get("payload")
        if isinstance(payload_data, dict) and hasattr(payload_type, "from_dict"):
            payload = payload_type.from_dict(payload_data)
        else:
            payload = payload_data
        return cls(
            schema_version=int(decoded["schema_version"]),
            schema_hash=str(decoded["schema_hash"]),
            subject=str(decoded["subject"]),
            idempotency_key=str(decoded["idempotency_key"]),
            payload_type=str(decoded["payload_type"]),
            produced_unix_ms=int(decoded["produced_unix_ms"]),
            headers=dict(decoded.get("headers") or {}),
            payload=payload,
        )
