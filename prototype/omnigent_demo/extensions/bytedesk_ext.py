"""EXTENSION (third-party) — the ``bytedesk_omnigent`` analog.

Lives entirely outside core. It uses the *same* SDK and the *same* contract the
first-party extensions use — proof that "core" has no privileged powers. It:

  * depends on the core ``ArtifactStore`` interface (injected — it neither knows
    nor cares which impl core wired);
  * overrides the ``Clock`` service with its own implementation, demonstrating
    that *any* part of the application can be replaced by re-registering the
    interface;
  * adds its own ``audit`` tool and a background task.
"""

from __future__ import annotations

import logging

from ..core.interfaces import ArtifactStore, Clock
from ..sdk import background, extension, provides, tool

logger = logging.getLogger("omnigent_demo.ext.bytedesk")


class TenantClock:
    """A replacement Clock — tenant-aware timestamps."""

    def now(self) -> str:
        return "2026-06-25T00:00:00Z[tenant=acme]"


class AuditTool:
    name = "audit"

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def __call__(self) -> list[str]:
        return self._store.keys()


@extension(name="bytedesk", requires=("core.stores", "core.tools"))
class BytedeskExtension:
    @provides(Clock)  # re-registers the Clock interface → replaces core's impl
    def tenant_clock(self) -> Clock:
        return TenantClock()

    @tool(name="audit")
    def audit_tool(self, store: ArtifactStore) -> AuditTool:
        return AuditTool(store)

    @background
    def heartbeat(self):
        async def _run() -> None:
            logger.info("bytedesk heartbeat tick")

        return _run
