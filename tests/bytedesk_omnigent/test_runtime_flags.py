from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.runtime_flags.extension import BytedeskRuntimeFlagsExtension
from bytedesk_omnigent.runtime_flags.models import (
    EvaluationContext,
    FlagDefinition,
    FlagDescriptor,
    FlagRule,
    FlagVariation,
    PercentageRollout,
    RolloutBucket,
)
from bytedesk_omnigent.runtime_flags.router import create_runtime_flags_router
from bytedesk_omnigent.runtime_flags.store import (
    FlagConflictError,
    InMemoryRuntimeFlagStore,
    set_runtime_flag_store_for_tests,
)
from omnigent.errors import OmnigentError
from omnigent.kernel.extensions import OmnigentExtension
from omnigent.sdk.contrib import CONTRIB_ATTR


def test_runtime_flags_extension_uses_omnigent_sdk() -> None:
    store = InMemoryRuntimeFlagStore()
    set_runtime_flag_store_for_tests(store)
    ext = BytedeskRuntimeFlagsExtension()

    try:
        assert isinstance(ext, OmnigentExtension)
        assert ext.name == "bytedesk.runtime_flags"
        assert ext.runtime_flag_store() is store
        assert len(ext.routers()) == 1
        assert getattr(BytedeskRuntimeFlagsExtension.runtime_flags_router, CONTRIB_ATTR)[
            "seam"
        ] == "router"
    finally:
        set_runtime_flag_store_for_tests(None)


@pytest.mark.asyncio
async def test_runtime_flag_store_enforces_revisioned_writes() -> None:
    store = InMemoryRuntimeFlagStore()
    flag = FlagDefinition(
        descriptor=FlagDescriptor(
            key="runtime.transport.nats",
            value_type="boolean",
            owner="runtime",
            default_value=False,
            off_value=False,
            description="Use the NATS runtime transport.",
        ),
        enabled=True,
        variations=(FlagVariation(key="on", value=True), FlagVariation(key="off", value=False)),
        default_variation="off",
    )

    created = await store.upsert(flag)
    updated = await store.upsert(flag.with_default_variation("on"), if_match=created.revision)

    assert created.revision == 1
    assert updated.revision == 2
    assert (await store.get("runtime.transport.nats")).default_variation == "on"
    with pytest.raises(FlagConflictError):
        await store.upsert(flag.with_default_variation("off"), if_match=created.revision)
    assert [entry.revision for entry in await store.history("runtime.transport.nats")] == [1, 2]


@pytest.mark.asyncio
async def test_evaluator_applies_targeting_prerequisites_and_rollout() -> None:
    store = InMemoryRuntimeFlagStore()
    gate = FlagDefinition(
        descriptor=FlagDescriptor(
            key="runtime.transport.enabled",
            value_type="boolean",
            owner="runtime",
            default_value=False,
            off_value=False,
        ),
        enabled=True,
        variations=(FlagVariation("on", True), FlagVariation("off", False)),
        default_variation="on",
    )
    flag = FlagDefinition(
        descriptor=FlagDescriptor(
            key="runtime.transport.mode",
            value_type="string",
            owner="runtime",
            default_value="ws",
            off_value="ws",
        ),
        enabled=True,
        variations=(
            FlagVariation("ws", "ws"),
            FlagVariation("nats", "nats"),
            FlagVariation("disabled", "disabled"),
        ),
        default_variation="ws",
        prerequisites={"runtime.transport.enabled": True},
        rules=(
            FlagRule(attribute="tenant", op="equals", values=("internal",), variation="nats"),
        ),
        rollout=PercentageRollout(
            attribute="session",
            buckets=(
                RolloutBucket(variation="nats", weight=50_000),
                RolloutBucket(variation="ws", weight=50_000),
            ),
        ),
    )
    await store.upsert(gate)
    await store.upsert(flag)

    targeted = await store.evaluate(
        "runtime.transport.mode",
        EvaluationContext(attributes={"tenant": "internal", "session": "s1"}),
    )
    rolled_a = await store.evaluate(
        "runtime.transport.mode",
        EvaluationContext(attributes={"tenant": "external", "session": "stable-session"}),
    )
    rolled_b = await store.evaluate(
        "runtime.transport.mode",
        EvaluationContext(attributes={"tenant": "external", "session": "stable-session"}),
    )

    assert targeted.value == "nats"
    assert targeted.reason == "rule_match"
    assert rolled_a.value == rolled_b.value
    assert rolled_a.reason == "percentage_rollout"

    await store.upsert(gate.with_default_variation("off"))
    blocked = await store.evaluate(
        "runtime.transport.mode",
        EvaluationContext(attributes={"tenant": "internal", "session": "s1"}),
    )
    assert blocked.value == "ws"
    assert blocked.reason == "prerequisite_failed"


@pytest.mark.asyncio
async def test_evaluator_allows_shared_prerequisites_without_cycle() -> None:
    store = InMemoryRuntimeFlagStore()

    def _flag(key: str, prerequisites: dict[str, bool] | None = None) -> FlagDefinition:
        return FlagDefinition(
            descriptor=FlagDescriptor(
                key=key,
                value_type="boolean",
                owner="runtime",
                default_value=True,
                off_value=False,
            ),
            enabled=True,
            variations=(FlagVariation("on", True), FlagVariation("off", False)),
            default_variation="on",
            prerequisites=prerequisites or {},
        )

    await store.upsert(_flag("leaf"))
    await store.upsert(_flag("left", {"leaf": True}))
    await store.upsert(_flag("right", {"leaf": True}))
    await store.upsert(_flag("root", {"left": True, "right": True}))

    result = await store.evaluate("root", EvaluationContext(attributes={}))

    assert result.value is True
    assert result.reason == "default"


def test_runtime_flags_router_crud_and_evaluate() -> None:
    store = InMemoryRuntimeFlagStore()
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(_request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(create_runtime_flags_router(store=store), prefix="/v1")
    client = TestClient(app)

    created = client.post(
        "/v1/flags",
        json={
            "key": "runtime.transport.nats",
            "value_type": "boolean",
            "owner": "runtime",
            "default_value": False,
            "off_value": False,
            "description": "Use NATS transport.",
            "enabled": True,
            "variations": [{"key": "on", "value": True}, {"key": "off", "value": False}],
            "default_variation": "on",
        },
    )
    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'

    fetched = client.get("/v1/flags/runtime.transport.nats")
    assert fetched.status_code == 200
    assert fetched.headers["etag"] == '"1"'

    evaluated = client.post(
        "/v1/flags/runtime.transport.nats/evaluate",
        json={"attributes": {"tenant": "internal"}},
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["value"] is True
    assert evaluated.json()["reason"] == "default"

    stale = client.patch(
        "/v1/flags/runtime.transport.nats",
        headers={"if-match": '"1"'},
        json={"default_variation": "off"},
    )
    assert stale.status_code == 200
    assert stale.headers["etag"] == '"2"'

    conflict = client.patch(
        "/v1/flags/runtime.transport.nats",
        headers={"if-match": '"1"'},
        json={"default_variation": "on"},
    )
    assert conflict.status_code == 412
