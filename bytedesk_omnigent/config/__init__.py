"""ByteDesk's Configuration-Control-Plane descriptors (ADR-0150, BDP-2413).

Contributed to the Settings Registry via ``BytedeskExtension.config_descriptors()``.
This is the first read-path set — representative descriptors across scopes,
tiers, storage substrates, and sensitivity — that proves the spine + secret
redaction. The full ~136-property catalog is filled in as later phases wire
each store's reader/writer (the write port is BDP-2414/2417).
"""

from __future__ import annotations

from omnigent.config import ConfigCtx, ConfigDescriptor, EnvConfigStore

_STR = {"type": "string"}


def bytedesk_config_descriptors() -> list[ConfigDescriptor]:
    """The first set of ByteDesk config descriptors (read-only spine)."""

    def _loaded_extensions(_ctx: ConfigCtx) -> list[str]:
        from omnigent.extensions import discover_extensions

        return sorted(ext.name for ext in discover_extensions())

    return [
        ConfigDescriptor(
            key="system.log_level",
            scope="system",
            what="Server log level (OMNIGENT_LOG_LEVEL).",
            json_schema={"type": "string", "enum": ["DEBUG", "INFO", "WARNING", "ERROR"]},
            tier=2,
            storage_source="env",
            reader=EnvConfigStore.reader("OMNIGENT_LOG_LEVEL"),
            effect_timing="requires_restart",
        ),
        ConfigDescriptor(
            key="system.nats.url",
            scope="system",
            what="NATS JetStream coordination/artifact backplane URL.",
            json_schema=_STR,
            tier=0,
            storage_source="env",
            reader=EnvConfigStore.reader("OMNIGENT_NATS_URL"),
            effect_timing="requires_restart",
            read_only_reason=(
                "deploy-only — flipping the backplane live desyncs coordination "
                "across pods (ADR-0148)"
            ),
        ),
        ConfigDescriptor(
            key="system.database.uri",
            scope="system",
            what="Primary SQLAlchemy database URI.",
            json_schema=_STR,
            tier=0,
            storage_source="env",
            reader=EnvConfigStore.reader("OMNIGENT_DATABASE_URI"),
            sensitivity="secret",
            effect_timing="requires_restart",
            read_only_reason=(
                "deploy-only — a live repoint split-brains sessions/signal-bus/"
                "idempotency across pods"
            ),
        ),
        ConfigDescriptor(
            key="system.infisical.client_secret",
            scope="system",
            what="Infisical universal-auth client secret (presence only).",
            json_schema=_STR,
            tier=0,
            storage_source="env",
            reader=EnvConfigStore.reader("OMNIGENT_INFISICAL_CLIENT_SECRET"),
            sensitivity="secret",
            read_only_reason="secret — set via Infisical/deploy, never the value over REST",
        ),
        ConfigDescriptor(
            key="system.extensions.loaded",
            scope="system",
            what="Extensions discovered + installed at boot (the kernel's plugins).",
            json_schema={"type": "array", "items": {"type": "string"}},
            tier=0,
            storage_source="memory",
            reader=_loaded_extensions,
            read_only_reason="derived from entry-points/OMNIGENT_EXTENSIONS at boot",
        ),
    ]
