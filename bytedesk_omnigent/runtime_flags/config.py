"""Config-control-plane descriptors for runtime flags."""

from __future__ import annotations

import os

from omnigent.config import ConfigCtx, ConfigDescriptor, EnvConfigStore


def runtime_flag_config_descriptors() -> list[ConfigDescriptor]:
    def _store_mode(_ctx: ConfigCtx) -> str:
        return os.environ.get("OMNIGENT_RUNTIME_FLAGS_STORE", "nats")

    return [
        ConfigDescriptor(
            key="runtime.flags.store",
            scope="system",
            what="Runtime feature flag store implementation. Production value is 'nats'.",
            json_schema={"type": "string", "enum": ["nats", "memory"]},
            tier=0,
            storage_source="memory",
            reader=_store_mode,
            effect_timing="requires_restart",
            read_only_reason="deploy-only - runtime flags require one shared NATS serving store",
        ),
        ConfigDescriptor(
            key="runtime.flags.nats.url",
            scope="system",
            what="NATS URL used by the runtime feature flag serving store.",
            json_schema={"type": "string"},
            tier=0,
            storage_source="env",
            reader=EnvConfigStore.reader("OMNIGENT_NATS_URL"),
            effect_timing="requires_restart",
            read_only_reason="deploy-only - changing the flag store URL live splits evaluators",
        ),
    ]
