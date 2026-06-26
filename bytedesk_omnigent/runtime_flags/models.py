"""Typed runtime flag contracts and local evaluation rules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

FLAG_VALUE_TYPES = {"boolean", "string", "number", "json"}
FLAG_LIFECYCLES = {"proposed", "active", "default_on", "sunset", "archived"}


class FlagValidationError(ValueError):
    """A flag definition is malformed or has an invalid value."""


@dataclass(frozen=True)
class FlagDescriptor:
    key: str
    value_type: str
    owner: str
    default_value: Any
    off_value: Any | None = None
    description: str = ""
    lifecycle: str = "active"
    safety_tier: int = 2
    tags: tuple[str, ...] = ()
    json_schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key or not self.key.strip():
            raise FlagValidationError("flag key is required")
        if self.value_type not in FLAG_VALUE_TYPES:
            raise FlagValidationError(
                f"{self.key}: value_type must be one of {sorted(FLAG_VALUE_TYPES)}"
            )
        if self.lifecycle not in FLAG_LIFECYCLES:
            raise FlagValidationError(
                f"{self.key}: lifecycle must be one of {sorted(FLAG_LIFECYCLES)}"
            )
        validate_value(self.key, self.value_type, self.default_value)
        if self.off_value is not None:
            validate_value(self.key, self.value_type, self.off_value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value_type": self.value_type,
            "owner": self.owner,
            "default_value": self.default_value,
            "off_value": self.off_value,
            "description": self.description,
            "lifecycle": self.lifecycle,
            "safety_tier": self.safety_tier,
            "tags": list(self.tags),
            "json_schema": self.json_schema,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlagDescriptor:
        return cls(
            key=str(data["key"]),
            value_type=str(data["value_type"]),
            owner=str(data.get("owner") or "runtime"),
            default_value=data.get("default_value"),
            off_value=data.get("off_value"),
            description=str(data.get("description") or ""),
            lifecycle=str(data.get("lifecycle") or "active"),
            safety_tier=int(data.get("safety_tier") or 2),
            tags=tuple(str(tag) for tag in data.get("tags") or ()),
            json_schema=dict(data.get("json_schema") or {}),
        )


@dataclass(frozen=True)
class FlagVariation:
    key: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlagVariation:
        return cls(key=str(data["key"]), value=data.get("value"))


@dataclass(frozen=True)
class FlagRule:
    attribute: str
    op: str
    values: tuple[Any, ...]
    variation: str

    def matches(self, context: EvaluationContext) -> bool:
        actual = context.attributes.get(self.attribute)
        if self.op == "equals":
            return actual in self.values
        if self.op == "not_equals":
            return actual not in self.values
        if self.op == "contains":
            if actual is None:
                return False
            if isinstance(actual, str | list | tuple | set | dict):
                return any(value in actual for value in self.values)
            return actual in self.values
        raise FlagValidationError(f"unsupported flag rule op {self.op!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attribute": self.attribute,
            "op": self.op,
            "values": list(self.values),
            "variation": self.variation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlagRule:
        return cls(
            attribute=str(data["attribute"]),
            op=str(data.get("op") or "equals"),
            values=tuple(data.get("values") or ()),
            variation=str(data["variation"]),
        )


@dataclass(frozen=True)
class RolloutBucket:
    variation: str
    weight: int

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise FlagValidationError("rollout bucket weight must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"variation": self.variation, "weight": self.weight}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RolloutBucket:
        return cls(variation=str(data["variation"]), weight=int(data["weight"]))


@dataclass(frozen=True)
class PercentageRollout:
    attribute: str
    buckets: tuple[RolloutBucket, ...]

    def choose(self, flag_key: str, context: EvaluationContext) -> str | None:
        raw = context.attributes.get(self.attribute)
        if raw is None:
            return None
        total = sum(bucket.weight for bucket in self.buckets)
        if total <= 0:
            return None
        point = stable_bucket(flag_key, str(raw))
        cursor = 0
        for bucket in self.buckets:
            cursor += bucket.weight
            if point < cursor:
                return bucket.variation
        return self.buckets[-1].variation if point < total else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attribute": self.attribute,
            "buckets": [bucket.to_dict() for bucket in self.buckets],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PercentageRollout | None:
        if not data:
            return None
        return cls(
            attribute=str(data["attribute"]),
            buckets=tuple(RolloutBucket.from_dict(item) for item in data.get("buckets") or ()),
        )


@dataclass(frozen=True)
class FlagDefinition:
    descriptor: FlagDescriptor
    enabled: bool = True
    variations: tuple[FlagVariation, ...] = ()
    default_variation: str | None = None
    targets: dict[str, str] = field(default_factory=dict)
    rules: tuple[FlagRule, ...] = ()
    rollout: PercentageRollout | None = None
    prerequisites: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        variations = self.variations or (
            FlagVariation("default", self.descriptor.default_value),
        )
        object.__setattr__(self, "variations", variations)
        if self.default_variation is None:
            object.__setattr__(self, "default_variation", variations[0].key)
        variation_map = self.variation_map()
        if self.default_variation not in variation_map:
            raise FlagValidationError(
                f"{self.descriptor.key}: default variation {self.default_variation!r} missing"
            )
        for variation in variations:
            validate_value(self.descriptor.key, self.descriptor.value_type, variation.value)
        for variation in self.targets.values():
            if variation not in variation_map:
                raise FlagValidationError(
                    f"{self.descriptor.key}: target variation {variation!r} missing"
                )
        for rule in self.rules:
            if rule.variation not in variation_map:
                raise FlagValidationError(
                    f"{self.descriptor.key}: rule variation {rule.variation!r} missing"
                )
        if self.rollout is not None:
            for bucket in self.rollout.buckets:
                if bucket.variation not in variation_map:
                    raise FlagValidationError(
                        f"{self.descriptor.key}: rollout variation {bucket.variation!r} missing"
                    )

    @property
    def key(self) -> str:
        return self.descriptor.key

    def variation_map(self) -> dict[str, Any]:
        return {variation.key: variation.value for variation in self.variations}

    def value_for_variation(self, variation: str | None) -> Any:
        if variation is None:
            return self.descriptor.default_value
        return self.variation_map()[variation]

    def off_value(self) -> Any:
        return (
            self.descriptor.off_value
            if self.descriptor.off_value is not None
            else self.descriptor.default_value
        )

    def with_default_variation(self, variation: str) -> FlagDefinition:
        return replace(self, default_variation=variation)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.descriptor.to_dict(),
            "enabled": self.enabled,
            "variations": [variation.to_dict() for variation in self.variations],
            "default_variation": self.default_variation,
            "targets": self.targets,
            "rules": [rule.to_dict() for rule in self.rules],
            "rollout": self.rollout.to_dict() if self.rollout else None,
            "prerequisites": self.prerequisites,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlagDefinition:
        descriptor = FlagDescriptor.from_dict(data)
        return cls(
            descriptor=descriptor,
            enabled=bool(data.get("enabled", True)),
            variations=tuple(
                FlagVariation.from_dict(item) for item in data.get("variations") or ()
            ),
            default_variation=data.get("default_variation"),
            targets={str(k): str(v) for k, v in dict(data.get("targets") or {}).items()},
            rules=tuple(FlagRule.from_dict(item) for item in data.get("rules") or ()),
            rollout=PercentageRollout.from_dict(data.get("rollout")),
            prerequisites=dict(data.get("prerequisites") or {}),
        )


@dataclass(frozen=True)
class EvaluationContext:
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EvaluationContext:
        return cls(attributes=dict((data or {}).get("attributes") or {}))


@dataclass(frozen=True)
class EvaluationResult:
    key: str
    value: Any
    variation: str | None
    reason: str
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "variation": self.variation,
            "reason": self.reason,
            "revision": self.revision,
        }


def stable_bucket(flag_key: str, attribute_value: str) -> int:
    """Return a stable rollout bucket in 0..99_999."""
    digest = hashlib.sha256(f"{flag_key}:{attribute_value}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100_000


def validate_value(key: str, value_type: str, value: Any) -> None:
    if value_type == "boolean":
        ok = isinstance(value, bool)
    elif value_type == "string":
        ok = isinstance(value, str)
    elif value_type == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        try:
            json.dumps(value)
            ok = True
        except TypeError:
            ok = False
    if not ok:
        raise FlagValidationError(
            f"{key}: expected {value_type}, got {type(value).__name__}"
        )
