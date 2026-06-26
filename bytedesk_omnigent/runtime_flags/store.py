"""Runtime flag store contracts and NATS-backed implementation."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from omnigent.errors import ErrorCode, OmnigentError

from .models import (
    EvaluationContext,
    EvaluationResult,
    FlagDefinition,
    FlagValidationError,
)

FLAG_DEFINITIONS_BUCKET = "OMNIGENT_FLAG_DEFINITIONS"
FLAG_EVENTS_SUBJECT = "omnigent.flags.changed"


class FlagNotFoundError(OmnigentError):
    def __init__(self, key: str) -> None:
        super().__init__(f"runtime flag {key!r} not found", code=ErrorCode.NOT_FOUND)


class FlagConflictError(OmnigentError):
    def __init__(self, key: str, expected: int | None, current: int) -> None:
        super().__init__(
            f"{key}: If-Match {expected!r} is stale (current {current})",
            code=ErrorCode.PRECONDITION_FAILED,
        )


class FlagUnavailableError(OmnigentError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.INTERNAL_ERROR)


@dataclass(frozen=True)
class FlagRevision:
    definition: FlagDefinition
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, **self.definition.to_dict()}


@runtime_checkable
class RuntimeFlagStore(Protocol):
    async def list(self) -> list[FlagRevision]: ...

    async def get(self, key: str) -> FlagDefinition: ...

    async def get_revision(self, key: str) -> FlagRevision: ...

    async def upsert(
        self,
        definition: FlagDefinition,
        *,
        if_match: int | None = None,
    ) -> FlagRevision: ...

    async def history(self, key: str) -> list[FlagRevision]: ...

    async def evaluate(
        self,
        key: str,
        context: EvaluationContext,
    ) -> EvaluationResult: ...

    async def changes(self) -> AsyncIterator[bytes]: ...


class _EvaluationMixin:
    async def evaluate(self, key: str, context: EvaluationContext) -> EvaluationResult:
        return await self._evaluate(key, context, seen=set())

    async def _evaluate(
        self,
        key: str,
        context: EvaluationContext,
        *,
        seen: set[str],
    ) -> EvaluationResult:
        if key in seen:
            raise OmnigentError(
                f"runtime flag prerequisite cycle at {key!r}",
                code=ErrorCode.CONFLICT,
            )
        path = {*seen, key}
        revision = await self.get_revision(key)  # type: ignore[attr-defined]
        flag = revision.definition
        if not flag.enabled:
            return EvaluationResult(
                key=key,
                value=flag.off_value(),
                variation=None,
                reason="off",
                revision=revision.revision,
            )
        for prereq_key, expected in flag.prerequisites.items():
            prereq = await self._evaluate(prereq_key, context, seen=path)
            if prereq.value != expected:
                return EvaluationResult(
                    key=key,
                    value=flag.off_value(),
                    variation=None,
                    reason="prerequisite_failed",
                    revision=revision.revision,
                )
        context_key = context.attributes.get("key") or context.attributes.get("user")
        if context_key is not None:
            variation = flag.targets.get(str(context_key))
            if variation is not None:
                return EvaluationResult(
                    key=key,
                    value=flag.value_for_variation(variation),
                    variation=variation,
                    reason="target_match",
                    revision=revision.revision,
                )
        for rule in flag.rules:
            if rule.matches(context):
                return EvaluationResult(
                    key=key,
                    value=flag.value_for_variation(rule.variation),
                    variation=rule.variation,
                    reason="rule_match",
                    revision=revision.revision,
                )
        if flag.rollout is not None:
            variation = flag.rollout.choose(flag.key, context)
            if variation is not None:
                return EvaluationResult(
                    key=key,
                    value=flag.value_for_variation(variation),
                    variation=variation,
                    reason="percentage_rollout",
                    revision=revision.revision,
                )
        return EvaluationResult(
            key=key,
            value=flag.value_for_variation(flag.default_variation),
            variation=flag.default_variation,
            reason="default",
            revision=revision.revision,
        )


class InMemoryRuntimeFlagStore(_EvaluationMixin):
    """Test/runtime-local store with the same revision contract as NATS KV."""

    def __init__(self) -> None:
        self._history: dict[str, list[FlagRevision]] = {}
        self._changes: list[bytes] = []

    async def list(self) -> list[FlagRevision]:
        return [items[-1] for _key, items in sorted(self._history.items()) if items]

    async def get(self, key: str) -> FlagDefinition:
        return (await self.get_revision(key)).definition

    async def get_revision(self, key: str) -> FlagRevision:
        revisions = self._history.get(key)
        if not revisions:
            raise FlagNotFoundError(key)
        return revisions[-1]

    async def upsert(
        self,
        definition: FlagDefinition,
        *,
        if_match: int | None = None,
    ) -> FlagRevision:
        _validate_definition(definition)
        revisions = self._history.setdefault(definition.key, [])
        current = revisions[-1].revision if revisions else 0
        if if_match is not None and if_match != current:
            raise FlagConflictError(definition.key, if_match, current)
        revision = FlagRevision(definition=definition, revision=current + 1)
        revisions.append(revision)
        self._changes.append(_encode_change(revision))
        return revision

    async def history(self, key: str) -> list[FlagRevision]:
        revisions = self._history.get(key)
        if not revisions:
            raise FlagNotFoundError(key)
        return list(revisions)

    async def changes(self) -> AsyncIterator[bytes]:
        for change in self._changes:
            yield change


class NatsRuntimeFlagStore(_EvaluationMixin):
    """NATS JetStream KV implementation for live runtime flags."""

    def __init__(
        self,
        nats_url: str,
        *,
        definitions_bucket: str = FLAG_DEFINITIONS_BUCKET,
    ) -> None:
        self._nats_url = nats_url
        self._definitions_bucket = definitions_bucket
        self._nc: Any = None
        self._js: Any = None
        self._kv: Any = None

    async def start(self) -> None:
        if self._kv is not None:
            return
        if not self._nats_url.strip():
            raise FlagUnavailableError("OMNIGENT_NATS_URL is required for runtime flags")
        try:
            import nats
            from nats.js.api import KeyValueConfig
        except ImportError as exc:
            raise FlagUnavailableError(
                "nats-py is required for runtime flags; install omnigent[coordination]"
            ) from exc
        self._nc = await nats.connect(
            servers=[self._nats_url],
            name="omnigent-runtime-flags",
            max_reconnect_attempts=-1,
        )
        self._js = self._nc.jetstream()
        try:
            self._kv = await self._js.key_value(self._definitions_bucket)
        except Exception:  # noqa: BLE001 - bucket may not exist
            self._kv = await self._js.create_key_value(
                config=KeyValueConfig(bucket=self._definitions_bucket, history=64)
            )

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            await self._nc.close()
        self._nc = None
        self._js = None
        self._kv = None

    async def list(self) -> list[FlagRevision]:
        await self.start()
        out: list[FlagRevision] = []
        try:
            keys = await self._kv.keys()
        except Exception:  # noqa: BLE001 - empty bucket
            return []
        for key in sorted(keys):
            if isinstance(key, str):
                out.append(await self.get_revision(key))
        return out

    async def get(self, key: str) -> FlagDefinition:
        return (await self.get_revision(key)).definition

    async def get_revision(self, key: str) -> FlagRevision:
        await self.start()
        try:
            entry = await self._kv.get(key)
        except Exception as exc:
            raise FlagNotFoundError(key) from exc
        return _decode_revision(entry.value, int(entry.revision))

    async def upsert(
        self,
        definition: FlagDefinition,
        *,
        if_match: int | None = None,
    ) -> FlagRevision:
        await self.start()
        _validate_definition(definition)
        payload = _encode_definition(definition)
        try:
            if if_match is None:
                revision = await self._kv.put(definition.key, payload)
            else:
                revision = await self._kv.update(definition.key, payload, last=if_match)
        except Exception as exc:
            current = 0
            with contextlib.suppress(OmnigentError):
                current = (await self.get_revision(definition.key)).revision
            raise FlagConflictError(definition.key, if_match, current) from exc
        written = FlagRevision(definition=definition, revision=int(revision))
        if self._nc is not None:
            await self._nc.publish(FLAG_EVENTS_SUBJECT, _encode_change(written))
        return written

    async def history(self, key: str) -> list[FlagRevision]:
        await self.start()
        try:
            entries = await self._kv.history(key)
        except Exception as exc:
            raise FlagNotFoundError(key) from exc
        return [_decode_revision(entry.value, int(entry.revision)) for entry in entries]

    async def changes(self) -> AsyncIterator[bytes]:
        await self.start()
        sub = await self._nc.subscribe(FLAG_EVENTS_SUBJECT)
        try:
            async for msg in sub.messages:
                yield msg.data
        finally:
            await sub.unsubscribe()


_STORE: RuntimeFlagStore | None = None


def runtime_flag_store_from_env() -> RuntimeFlagStore:
    """Return the process runtime flag store.

    Production defaults to NATS. The in-memory store exists for unit tests and
    explicit local overrides only; it is not a legacy transport fallback.
    """
    global _STORE
    if _STORE is not None:
        return _STORE
    mode = os.environ.get("OMNIGENT_RUNTIME_FLAGS_STORE", "nats").strip().lower()
    if mode == "memory":
        _STORE = InMemoryRuntimeFlagStore()
    elif mode == "nats":
        _STORE = NatsRuntimeFlagStore(os.environ.get("OMNIGENT_NATS_URL", ""))
    else:
        raise FlagUnavailableError(
            "OMNIGENT_RUNTIME_FLAGS_STORE must be 'nats' or 'memory'"
        )
    return _STORE


def set_runtime_flag_store_for_tests(store: RuntimeFlagStore | None) -> None:
    global _STORE
    _STORE = store


def _validate_definition(definition: FlagDefinition) -> None:
    try:
        FlagDefinition.from_dict(definition.to_dict())
    except FlagValidationError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc


def _encode_definition(definition: FlagDefinition) -> bytes:
    return json.dumps(definition.to_dict(), separators=(",", ":")).encode("utf-8")


def _decode_revision(raw: bytes, revision: int) -> FlagRevision:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise FlagUnavailableError("runtime flag payload must be a JSON object")
    return FlagRevision(definition=FlagDefinition.from_dict(data), revision=revision)


def _encode_change(revision: FlagRevision) -> bytes:
    return json.dumps(
        {
            "key": revision.definition.key,
            "revision": revision.revision,
            "lifecycle": revision.definition.descriptor.lifecycle,
        },
        separators=(",", ":"),
    ).encode("utf-8")
