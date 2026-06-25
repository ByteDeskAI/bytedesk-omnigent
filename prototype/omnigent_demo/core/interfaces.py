"""CORE — capability interfaces extensions depend on (Dependency Inversion).

An extension depends on these Protocols, never on a concrete class. The DI
container is registered *by interface*, so the implementation can be swapped
(in-memory ↔ S3) without any consumer changing. This is the seam that makes the
``OMNIGENT_USE_<SEAM>`` strangler flag meaningful.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> str: ...


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, key: str, value: str) -> None: ...
    def get(self, key: str) -> str | None: ...
    def keys(self) -> list[str]: ...
