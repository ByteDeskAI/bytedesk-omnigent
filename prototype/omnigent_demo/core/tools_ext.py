"""CORE first-party extension — built-in tools.

The ``record`` tool depends on the ``ArtifactStore`` *interface* and a ``Clock``
*interface* — both injected by the DI container. It has no idea whether the
store is in-memory or "S3"; replaceability is total. This extension declares
``requires=("core.stores",)`` so boot fails fast and legibly if storage is absent.
"""

from __future__ import annotations

from ..sdk import extension, tool
from .interfaces import ArtifactStore, Clock


class RecordTool:
    """A tiny tool that timestamps and stores a value."""

    name = "record"

    def __init__(self, store: ArtifactStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def __call__(self, key: str, value: str) -> str:
        stamped = f"[{self._clock.now()}] {value}"
        self._store.put(key, stamped)
        return f"stored {key!r} -> {stamped!r}"


@extension(name="core.tools", requires=("core.stores",), entry_point=False)
class ToolsExtension:
    @tool(name="record")
    def record_tool(self, store: ArtifactStore, clock: Clock) -> RecordTool:
        # `store` and `clock` are injected from the container by interface.
        return RecordTool(store, clock)
