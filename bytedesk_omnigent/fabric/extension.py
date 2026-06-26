"""SDK-backed ByteDesk fabric extension."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter

from omnigent.sdk import background, extension, router

logger = logging.getLogger(__name__)


@extension(name="bytedesk.fabric", requires=("omnigent.coordination",))
class BytedeskFabricExtension:
    """Contribute fabric admin APIs and background hooks through the SDK."""

    @router()
    def fabric_router(
        self,
        auth_provider=None,
        permission_store=None,
    ) -> APIRouter:
        del permission_store
        from bytedesk_omnigent.routes.fabric import create_fabric_router

        return create_fabric_router(auth_provider=auth_provider)

    @background
    async def fabric_service_background(self) -> None:
        nats_url = os.environ.get("OMNIGENT_NATS_URL", "").strip()
        if not nats_url:
            logger.info("fabric background services disabled: OMNIGENT_NATS_URL is unset")
            return
        from bytedesk_omnigent.fabric.outbox import fabric_outbox_replay_loop
        from omnigent.fabric.manifest import DEFAULT_FABRIC_MANIFEST
        from omnigent.fabric.nats_adapter import NatsFabricAdapter
        from omnigent.fabric.service_host import create_required_fabric_service_hosts

        adapter = NatsFabricAdapter(nats_url)
        registrations = []
        try:
            await adapter.ensure_assets(DEFAULT_FABRIC_MANIFEST)
            for service in create_required_fabric_service_hosts().values():
                registrations.append(await adapter.serve_service_host(service))
            logger.info(
                "fabric services registered: count=%d",
                len(registrations),
            )
            logger.info("fabric outbox replay service starting")
            await fabric_outbox_replay_loop(nats_url=nats_url, adapter=adapter)
        finally:
            for registration in reversed(registrations):
                await registration.drain()
            await adapter.close()
