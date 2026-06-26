"""Short-lived runner credential store for fabric jobs."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable

from .models import CredentialReference, RunnerJob


def _unix_ms() -> int:
    return int(time.time() * 1000)


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
