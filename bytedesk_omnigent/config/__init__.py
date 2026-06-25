"""ByteDesk's Configuration-Control-Plane descriptors (ADR-0150, BDP-2413).

Contributed to the Settings Registry via ``BytedeskExtension.config_descriptors()``.
This is the first read-path set — representative descriptors across scopes,
tiers, storage substrates, and sensitivity — that proves the spine + secret
redaction. The full ~136-property catalog is filled in as later phases wire
each store's reader/writer (the write port is BDP-2414/2417).
"""

from __future__ import annotations

from omnigent.config import (
    ConfigCtx,
    ConfigDescriptor,
    ConfigFloorError,
    EnvConfigStore,
    runtime_store,
)

_STR = {"type": "string"}


def bytedesk_config_descriptors() -> list[ConfigDescriptor]:
    """The first set of ByteDesk config descriptors (read-only spine)."""

    def _loaded_extensions(_ctx: ConfigCtx) -> list[str]:
        from omnigent.kernel.extensions import discover_extensions

        return sorted(ext.name for ext in discover_extensions())

    # Live-tunable runtime descriptors (BDP-2414/2417): version = ETag,
    # write = If-Match compare-and-swap on the process runtime store.
    rt = runtime_store()

    def _rt_reader(key: str, default: object):
        return lambda _ctx: rt.get(key, default)

    def _rt_writer(key: str):
        def _write(value: object, if_match: str | None, _ctx: ConfigCtx) -> None:
            rt.set(key, value, if_match=if_match)

        return _write

    def _rt_etag(key: str):
        return lambda _ctx: str(rt.version(key))

    def _positive_cost(value: object) -> float:
        # Tier-1 floor: a non-positive / non-numeric ceiling can never trip.
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigFloorError("cost ceiling must be a positive number")
        return float(value)

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
        # ── writable (BDP-2414/2417) ──────────────────────────────────────────
        ConfigDescriptor(
            key="system.default_ad_hoc_model",
            scope="system",
            what="Default model for ad-hoc (no-agent) runs; live-tunable.",
            json_schema=_STR,
            tier=2,
            storage_source="memory",
            reader=_rt_reader("system.default_ad_hoc_model", "gpt-5.5"),
            writer=_rt_writer("system.default_ad_hoc_model"),
            etag_reader=_rt_etag("system.default_ad_hoc_model"),
            change_event="config.changed:system.default_ad_hoc_model",
        ),
        ConfigDescriptor(
            key="system.host.visibility_scope",
            scope="system",
            what=(
                "Who can see/use connected hosts: 'org-shared' (any authenticated "
                "member sees + uses all external hosts) or 'private' (per-owner "
                "isolation). Managed sandbox hosts stay owner-scoped in every "
                "mode (ADR-0151)."
            ),
            json_schema={"type": "string", "enum": ["org-shared", "private"]},
            tier=2,
            storage_source="memory",
            reader=_rt_reader("system.host.visibility_scope", "org-shared"),
            writer=_rt_writer("system.host.visibility_scope"),
            etag_reader=_rt_etag("system.host.visibility_scope"),
            change_event="config.changed:system.host.visibility_scope",
        ),
        ConfigDescriptor(
            key="policies.cost_hard_stop.default_ceiling_usd",
            scope="policy",
            what="Default hard cost ceiling (USD) seeded into new cost_hard_stop policies.",
            json_schema={"type": "number"},
            tier=1,
            storage_source="memory",
            reader=_rt_reader("policies.cost_hard_stop.default_ceiling_usd", 50.0),
            writer=_rt_writer("policies.cost_hard_stop.default_ceiling_usd"),
            etag_reader=_rt_etag("policies.cost_hard_stop.default_ceiling_usd"),
            floor=">0 (a non-positive ceiling can never trip the breaker)",
            floor_check=_positive_cost,
            change_event="config.changed:policies.cost_hard_stop.default_ceiling_usd",
        ),
    ]
