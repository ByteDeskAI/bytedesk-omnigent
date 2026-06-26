from __future__ import annotations

import pytest

from omnigent.fabric.credentials import InMemoryRunnerCredentialStore, NatsRunnerCredentialStore
from omnigent.fabric.models import CredentialReference, RunnerJob


def _job() -> RunnerJob:
    return RunnerJob(
        job_id="job_1",
        session_id="conv_1",
        tenant_id="tenant_1",
        org_id="org_1",
        lane="default",
        epoch=7,
        deadline_unix_ms=2_000,
        capacity={"cpu": 1},
        credential=CredentialReference(
            ref="bootstrap",
            principal_id="bootstrap",
            expires_unix_ms=2_000,
        ),
    )


@pytest.mark.asyncio
async def test_credential_store_mints_lookup_and_revokes() -> None:
    now = 1_000
    store = InMemoryRunnerCredentialStore(
        ttl_ms=500,
        now_ms=lambda: now,
        token_factory=lambda: "token",
    )

    credential = await store.mint(_job())

    assert credential.ref == "cred_token"
    assert credential.principal_id == "runner_job:job_1"
    assert credential.expires_unix_ms == 1_500
    assert credential.metadata["session_id"] == "conv_1"
    assert await store.lookup("cred_token") == credential

    await store.revoke("cred_token")

    assert await store.lookup("cred_token") is None


@pytest.mark.asyncio
async def test_credential_store_expires_on_lookup() -> None:
    now = 1_000

    def _now() -> int:
        return now

    store = InMemoryRunnerCredentialStore(
        ttl_ms=100,
        now_ms=_now,
        token_factory=lambda: "token",
    )
    await store.mint(_job())

    now = 1_101

    assert await store.lookup("cred_token") is None
    assert await store.records() == []


@pytest.mark.asyncio
async def test_credential_store_records_runner_launch_token() -> None:
    now = 1_000
    store = InMemoryRunnerCredentialStore(now_ms=lambda: now)

    credential = await store.record_launch_token(
        "runner_token_1",
        "alice@example.com",
        "binding-secret",
        ttl_ms=500,
    )

    assert credential.runner_id == "runner_token_1"
    assert credential.owner == "alice@example.com"
    assert credential.token == "binding-secret"
    assert credential.expires_unix_ms == 1_500
    assert await store.lookup_launch_token("runner_token_1") == credential

    now = 1_501

    assert await store.lookup_launch_token("runner_token_1") is None


class _FakeKvAdapter:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []

    async def kv_put(self, bucket: str, key: str, payload: bytes) -> None:
        self.records[(bucket, key)] = payload

    async def kv_get(self, bucket: str, key: str) -> bytes | None:
        return self.records.get((bucket, key))

    async def kv_delete(self, bucket: str, key: str) -> None:
        self.deleted.append((bucket, key))
        self.records.pop((bucket, key), None)


@pytest.mark.asyncio
async def test_nats_credential_store_round_trips_runner_launch_token() -> None:
    adapter = _FakeKvAdapter()
    store = NatsRunnerCredentialStore(  # type: ignore[arg-type]
        adapter,
        bucket="runner-creds",
        ttl_ms=500,
        now_ms=lambda: 1_000,
    )

    credential = await store.record_launch_token(
        "runner_token_1",
        "alice@example.com",
        "binding-secret",
    )

    assert credential.expires_unix_ms == 1_500
    assert await store.lookup_launch_token("runner_token_1") == credential

    await store.revoke_launch_token("runner_token_1")

    assert await store.lookup_launch_token("runner_token_1") is None
