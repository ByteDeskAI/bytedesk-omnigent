"""SDK-backed ByteDesk runtime flags extension."""

from __future__ import annotations

from fastapi import APIRouter

from omnigent.sdk import extension, provides, router

from .config import runtime_flag_config_descriptors
from .store import RuntimeFlagStore, runtime_flag_store_from_env


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
