"""SDK-backed ByteDesk runtime flags extension."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from omnigent.sdk import background, extension, provides, router

from .config import runtime_flag_config_descriptors
from .store import RuntimeFlagStore, runtime_flag_store_from_env

logger = logging.getLogger(__name__)


@extension(name="bytedesk.runtime_flags", requires=("omnigent.coordination",))
class BytedeskRuntimeFlagsExtension:
    """Contribute runtime flag APIs through the public Omnigent SDK."""

    @provides(RuntimeFlagStore)
    def runtime_flag_store(self) -> RuntimeFlagStore:
        return runtime_flag_store_from_env()

    @router()
    def runtime_flags_router(
        self,
        store: RuntimeFlagStore,
        auth_provider=None,
        permission_store=None,
    ) -> APIRouter:
        del permission_store
        from .router import create_runtime_flags_router

        return create_runtime_flags_router(
            auth_provider=auth_provider,
            store=store,
        )

    def config_descriptors(self) -> list:
        return runtime_flag_config_descriptors()

    @background
    async def _seed_runtime_flag_defaults(self) -> None:
        """Create the default rollout flags once without overwriting live edits."""
        from .defaults import seed_runtime_flag_defaults

        try:
            created = await seed_runtime_flag_defaults(runtime_flag_store_from_env())
        except Exception as exc:  # noqa: BLE001 - seed must not block server boot
            logger.warning("runtime flag default seed skipped: %s", exc)
            return
        if created:
            logger.info(
                "runtime flag default seed: created=%d keys=%s",
                len(created),
                ",".join(entry.definition.key for entry in created),
            )
