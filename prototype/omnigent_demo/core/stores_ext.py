"""CORE first-party extension — storage.

Demonstrates *interface-based replaceability*: this extension registers the
``ArtifactStore`` capability under its **interface** in the DI container. Two
concrete implementations are provided; which one is active is chosen by name, so
any consumer that depends on ``ArtifactStore`` gets whichever impl is wired —
swap it by changing one registration, touching zero consumers.

This is an ordinary extension using the ordinary SDK — there is no privileged
"core" wiring. (ServiceStack's built-in features are `IPlugin`s exactly like
this; "core" = the host plus a curated set of these.)
"""

from __future__ import annotations

import os

from ..sdk import extension, provides
from .interfaces import ArtifactStore, Clock


class SystemClock:
    def now(self) -> str:
        return "2026-06-25T00:00:00Z"  # fixed for deterministic demo output


class InMemoryArtifactStore:
    """Default impl — a dict."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self._data[key] = value

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def keys(self) -> list[str]:
        return list(self._data)


class FakeS3ArtifactStore:
    """Drop-in replacement impl — same interface, "remote" semantics."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self._data[f"s3://bucket/{key}"] = value

    def get(self, key: str) -> str | None:
        return self._data.get(f"s3://bucket/{key}")

    def keys(self) -> list[str]:
        return list(self._data)


@extension(name="core.stores", entry_point=False)
class StoresExtension:
    @provides(Clock)
    def clock(self) -> Clock:
        return SystemClock()

    @provides(ArtifactStore)
    def artifact_store(self) -> ArtifactStore:
        # Interface registered once; impl selected by the strangler flag.
        # `OMNIGENT_USE_ARTIFACT_STORE=s3` swaps it with no consumer change.
        impl = os.environ.get("OMNIGENT_USE_ARTIFACT_STORE", "memory").strip()
        return FakeS3ArtifactStore() if impl == "s3" else InMemoryArtifactStore()
