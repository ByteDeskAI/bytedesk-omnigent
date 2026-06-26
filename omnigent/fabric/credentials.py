"""Short-lived runner credential store for fabric jobs."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from .models import CredentialReference, RunnerJob
from .nats_adapter import NatsFabricAdapter


def _unix_ms() -> int:
    return int(time.time() * 1000)


_RUNNER_CREDENTIAL_BUCKET = "omnigent-fabric-runner-credentials"
_RUNNER_LAUNCH_CREDENTIAL_TTL_MS = 86_400_000


@dataclass(frozen=True)
class RunnerLaunchCredential:
    """Credential needed by any server replica to reach one runner over NATS."""

    runner_id: str
    owner: str
    token: str
    expires_unix_ms: int


class InMemoryRunnerCredentialStore:
    """Mint, revoke, and look up short-lived runner credential references."""

    def __init__(
        self,
        *,
        ttl_ms: int = 300_000,
        now_ms: Callable[[], int] = _unix_ms,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._ttl_ms = ttl_ms
        self._now_ms = now_ms
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(18))
        self._records: dict[str, CredentialReference] = {}
        self._launch_records: dict[str, RunnerLaunchCredential] = {}
        self._lock = asyncio.Lock()

    async def mint(self, job: RunnerJob) -> CredentialReference:
        ref = f"cred_{self._token_factory()}"
        credential = CredentialReference(
            ref=ref,
            principal_id=f"runner_job:{job.job_id}",
            expires_unix_ms=self._now_ms() + self._ttl_ms,
            scope="runner",
            metadata={
                "job_id": job.job_id,
                "session_id": job.session_id,
                "tenant_id": job.tenant_id,
                "org_id": job.org_id,
                "lane": job.lane,
                "epoch": job.epoch,
            },
        )
        async with self._lock:
            self._records[ref] = credential
        return credential

    async def revoke(self, ref: str) -> None:
        async with self._lock:
            self._records.pop(ref, None)

    async def lookup(self, ref: str) -> CredentialReference | None:
        async with self._lock:
            credential = self._records.get(ref)
            if credential is None:
                return None
            if credential.expires_unix_ms <= self._now_ms():
                self._records.pop(ref, None)
                return None
            return credential

    async def records(self) -> list[CredentialReference]:
        async with self._lock:
            return sorted(self._records.values(), key=lambda credential: credential.ref)

    async def record_launch_token(
        self,
        runner_id: str,
        owner: str,
        token: str,
        *,
        ttl_ms: int = _RUNNER_LAUNCH_CREDENTIAL_TTL_MS,
    ) -> RunnerLaunchCredential:
        credential = RunnerLaunchCredential(
            runner_id=runner_id,
            owner=owner,
            token=token,
            expires_unix_ms=self._now_ms() + ttl_ms,
        )
        async with self._lock:
            self._launch_records[runner_id] = credential
        return credential

    async def lookup_launch_token(self, runner_id: str) -> RunnerLaunchCredential | None:
        async with self._lock:
            credential = self._launch_records.get(runner_id)
            if credential is None:
                return None
            if credential.expires_unix_ms <= self._now_ms():
                self._launch_records.pop(runner_id, None)
                return None
            return credential

    async def revoke_launch_token(self, runner_id: str) -> None:
        async with self._lock:
            self._launch_records.pop(runner_id, None)


class NatsRunnerCredentialStore:
    """KV-backed runner credential store shared across server replicas."""

    def __init__(
        self,
        adapter: NatsFabricAdapter,
        *,
        bucket: str = _RUNNER_CREDENTIAL_BUCKET,
        ttl_ms: int = _RUNNER_LAUNCH_CREDENTIAL_TTL_MS,
        now_ms: Callable[[], int] = _unix_ms,
    ) -> None:
        self._adapter = adapter
        self._bucket = bucket
        self._ttl_ms = ttl_ms
        self._now_ms = now_ms

    async def record_launch_token(
        self,
        runner_id: str,
        owner: str,
        token: str,
        *,
        ttl_ms: int | None = None,
    ) -> RunnerLaunchCredential:
        credential = RunnerLaunchCredential(
            runner_id=runner_id,
            owner=owner,
            token=token,
            expires_unix_ms=self._now_ms() + (ttl_ms if ttl_ms is not None else self._ttl_ms),
        )
        await self._adapter.kv_put(
            self._bucket,
            runner_id,
            json.dumps(
                {
                    "runner_id": credential.runner_id,
                    "owner": credential.owner,
                    "token": credential.token,
                    "expires_unix_ms": credential.expires_unix_ms,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        return credential

    async def lookup_launch_token(self, runner_id: str) -> RunnerLaunchCredential | None:
        raw = await self._adapter.kv_get(self._bucket, runner_id)
        if raw is None:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
            credential = RunnerLaunchCredential(
                runner_id=str(data["runner_id"]),
                owner=str(data["owner"]),
                token=str(data["token"]),
                expires_unix_ms=int(data["expires_unix_ms"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self.revoke_launch_token(runner_id)
            return None
        if credential.runner_id != runner_id:
            await self.revoke_launch_token(runner_id)
            return None
        if credential.expires_unix_ms <= self._now_ms():
            await self.revoke_launch_token(runner_id)
            return None
        return credential

    async def revoke_launch_token(self, runner_id: str) -> None:
        await self._adapter.kv_delete(self._bucket, runner_id)


def create_runner_credential_store_from_env() -> (
    InMemoryRunnerCredentialStore | NatsRunnerCredentialStore
):
    """Create the runner credential store for this server process."""
    nats_url = os.environ.get("OMNIGENT_NATS_URL", "").strip()
    if nats_url:
        return NatsRunnerCredentialStore(NatsFabricAdapter(nats_url))
    return InMemoryRunnerCredentialStore()
