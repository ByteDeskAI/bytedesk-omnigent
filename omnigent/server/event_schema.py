"""Single source of the ServerStreamEvent JSON-Schema (BDP-2443, ADR-0152).

One builder consumed by three call sites — scripts/dump_openapi.py (the committed
snapshot), the live ``app.openapi()`` wrapper, and ``GET /v1/schema/events`` — so
the event contract the ByteDesk SDK generates from can never drift between them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import TypeAdapter

# Bumped only when the event-schema *shape contract* changes in a way consumers
# must notice (a structural break), independent of additive variant growth.
EVENT_SCHEMA_VERSION = "1"


def _union_schema(ref_template: str) -> dict[str, Any]:
    """Build the discriminated-union schema with the given ``$ref`` template.

    :returns: ``{"root": <oneOf+discriminator>, "definitions": <per-variant defs>}``.
    """
    from omnigent.server.schemas import ServerStreamEvent

    adapter: TypeAdapter[Any] = TypeAdapter(ServerStreamEvent)
    schema = adapter.json_schema(ref_template=ref_template)
    definitions = schema.pop("$defs", {})
    return {"root": schema, "definitions": definitions}


def server_stream_event_schema() -> dict[str, Any]:
    """Union schema in OpenAPI form (``$ref`` → ``#/components/schemas/<name>``)."""
    return _union_schema("#/components/schemas/{model}")


def inject_event_union(schemas: dict[str, Any]) -> None:
    """Merge the union root + per-variant defs into an OpenAPI ``components.schemas`` map.

    The root goes under ``ServerStreamEvent``; each variant def is added with
    ``setdefault`` so a schema FastAPI already synthesized (e.g. ``ResponseObject``)
    is not clobbered — the serialized shape is identical. Mutates ``schemas`` in place.
    """
    union = server_stream_event_schema()
    schemas["ServerStreamEvent"] = union["root"]
    for name, definition in union["definitions"].items():
        schemas.setdefault(name, definition)


def event_schema_document() -> dict[str, Any]:
    """Standalone, self-contained JSON-Schema document for ``GET /v1/schema/events``.

    Internal ``$ref`` pointers use ``#/$defs/<name>`` so the document validates on
    its own (the SDK's event-union codegen consumes this directly).
    """
    union = _union_schema("#/$defs/{model}")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ServerStreamEvent",
        "x-omnigent-event-schema-version": EVENT_SCHEMA_VERSION,
        **union["root"],
        "$defs": union["definitions"],
    }


def event_schema_hash() -> str:
    """Stable SHA-256 of the canonical event-schema document.

    Advertised on ``/v1/_capabilities`` so a client can detect at runtime whether
    its pinned snapshot still matches the server. Canonicalized (sorted keys, compact
    separators) so the hash is deterministic across processes.
    """
    canonical = json.dumps(
        event_schema_document(), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
