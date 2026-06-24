"""Coverage for the shared ServerStreamEvent schema builder (BDP-2443).

One builder (:mod:`omnigent.server.event_schema`) feeds three call sites — the
committed ``openapi.json`` snapshot, the live ``app.openapi()`` wrapper, and
``GET /v1/schema/events`` — so the event contract the ByteDesk SDK generates
from cannot drift between them. These tests pin:

1. The standalone document shape (``oneOf`` + ``type``-discriminator + ``$defs``).
2. That every wire ``type`` in :data:`_KNOWN_EVENT_TYPES` is in the
   discriminator ``mapping``.
3. That the standalone document self-refs (no ``#/components/schemas/`` leak).
4. Hash determinism + 64-char hex shape.
5. ``inject_event_union`` parity with ``server_stream_event_schema``.
6. The live endpoints (``/v1/schema/events`` unauthed; ``/v1/_capabilities``
   schema block) and the ``app.openapi()`` wrapper injection.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.server.event_schema import (
    event_schema_document,
    event_schema_hash,
    inject_event_union,
    server_stream_event_schema,
)
from omnigent.server.schemas import _KNOWN_EVENT_TYPES

# ── Part 1: standalone document shape ────────────────────────────


def test_event_schema_document_is_a_discriminated_union() -> None:
    """The document is a top-level ``oneOf`` keyed by the ``type`` discriminator.

    If the union root ever stopped serializing as ``oneOf`` +
    ``discriminator``, the SDK's typed-event codegen would have no
    branch to dispatch on — it would degrade to an untyped blob.
    """
    doc = event_schema_document()
    assert "oneOf" in doc, (
        "event_schema_document lost its top-level oneOf — the SDK can no "
        "longer enumerate the event variants."
    )
    assert isinstance(doc["oneOf"], list) and doc["oneOf"]
    discriminator = doc["discriminator"]
    assert discriminator["propertyName"] == "type", (
        f"discriminator must dispatch on the wire ``type`` field, got "
        f"{discriminator.get('propertyName')!r}."
    )
    assert isinstance(doc["$defs"], dict) and doc["$defs"], (
        "the standalone document must carry its per-variant defs under $defs."
    )


def test_every_known_event_type_is_in_the_discriminator_mapping() -> None:
    """Every wire type in the union is reachable via the discriminator mapping.

    ``_KNOWN_EVENT_TYPES`` is the runtime source of truth for accepted
    wire names; a type missing from the schema's ``mapping`` would mean a
    legitimate event the server emits has no entry the SDK can route on.
    """
    mapping = event_schema_document()["discriminator"]["mapping"]
    missing = {wire for wire in _KNOWN_EVENT_TYPES if wire not in mapping}
    assert not missing, (
        f"Wire types in _KNOWN_EVENT_TYPES absent from the schema "
        f"discriminator mapping: {sorted(missing)}. The schema builder "
        f"drifted from the union source of truth."
    )
    # Symmetric: the mapping must not invent types the union doesn't have.
    extra = set(mapping) - set(_KNOWN_EVENT_TYPES)
    assert not extra, (
        f"discriminator mapping has wire types not in _KNOWN_EVENT_TYPES: "
        f"{sorted(extra)}."
    )


def test_standalone_document_self_refs_via_defs_only() -> None:
    """The standalone document must not leak OpenAPI ``#/components/schemas/`` refs.

    ``GET /v1/schema/events`` is consumed as a self-contained JSON-Schema
    document — every internal ``$ref`` must resolve within the document
    (``#/$defs/...``). A ``#/components/schemas/`` pointer would dangle for
    any consumer that doesn't also hold the full OpenAPI spec.
    """
    serialized = json.dumps(event_schema_document())
    assert "#/components/schemas/" not in serialized, (
        "event_schema_document leaked an OpenAPI ``#/components/schemas/`` "
        "ref — the standalone document must self-ref via ``#/$defs/``."
    )
    # Positive proof it does self-ref the documented way.
    assert "#/$defs/" in serialized


# ── Part 2: hash determinism ─────────────────────────────────────


def test_event_schema_hash_is_deterministic_64_char_hex() -> None:
    """The advertised hash is a stable 64-char SHA-256 hex digest.

    A client pins this to detect at runtime whether its snapshot still
    matches the server. A non-deterministic hash (dict-order-dependent)
    would false-positive a drift on every process restart.
    """
    first = event_schema_hash()
    second = event_schema_hash()
    assert first == second, (
        "event_schema_hash is not deterministic across calls — the "
        "canonicalization (sort_keys) regressed."
    )
    assert len(first) == 64, f"expected a 64-char SHA-256 hex digest, got {len(first)}."
    # All hex — guards against an accidental non-hex encoding.
    int(first, 16)


# ── Part 3: inject_event_union parity ────────────────────────────


def test_inject_event_union_seeds_root_and_variant_defs() -> None:
    """``inject_event_union`` adds the root + per-variant defs into a schemas map.

    This is the exact mutation ``scripts/dump_openapi.py`` and the live
    ``app.openapi()`` wrapper both apply — its ``ServerStreamEvent`` entry
    must equal ``server_stream_event_schema()["root"]`` so the committed
    snapshot and the live spec describe the union identically.
    """
    schemas: dict = {}
    inject_event_union(schemas)
    assert "ServerStreamEvent" in schemas, (
        "inject_event_union must register the union root under "
        "``ServerStreamEvent``."
    )
    assert schemas["ServerStreamEvent"] == server_stream_event_schema()["root"], (
        "the injected root must match server_stream_event_schema()['root'] "
        "byte-for-byte — the three call sites share this exact value."
    )
    # Per-variant defs are merged alongside the root (not nested under it).
    variant_defs = {k for k in schemas if k != "ServerStreamEvent"}
    assert variant_defs, (
        "inject_event_union must also merge the per-variant component defs "
        "so the root's $refs resolve."
    )


# ── Part 4: live endpoints + app.openapi() wrapper ───────────────


@pytest.mark.asyncio
async def test_schema_events_endpoint_is_unauthed_and_typed(
    client: httpx.AsyncClient,
) -> None:
    """``GET /v1/schema/events`` returns the union document, unauthed (200).

    Mirrors ``/v1/info`` — the SDK fetches the event contract before
    holding a session, so the route must not require auth and must carry
    the discriminated-union shape.
    """
    resp = await client.get("/v1/schema/events")
    assert resp.status_code == 200, (
        f"GET /v1/schema/events should be unauthed 200, got "
        f"{resp.status_code}: {resp.text[:200]}"
    )
    body = resp.json()
    assert "oneOf" in body, "the events schema response lost its oneOf union."
    assert body["discriminator"]["propertyName"] == "type"


@pytest.mark.asyncio
async def test_capabilities_endpoint_advertises_event_schema_block(
    client: httpx.AsyncClient,
) -> None:
    """``GET /v1/_capabilities`` additively carries the event-schema block.

    The block lets a client detect schema drift without fetching the full
    document: ``hash`` must equal :func:`event_schema_hash`, and
    ``variant_count`` must equal the live union size. A 500 here would mean
    the additive return shape broke the route's response model.
    """
    resp = await client.get("/v1/_capabilities")
    assert resp.status_code == 200, (
        f"GET /v1/_capabilities should be 200, got {resp.status_code}: "
        f"{resp.text[:200]}"
    )
    body = resp.json()
    # Pre-existing seams manifest is untouched.
    assert "seams" in body
    events = body["schema"]["events"]
    assert events["hash"] == event_schema_hash(), (
        "the advertised schema hash must match event_schema_hash() so a "
        "client comparing against it gets a true drift signal."
    )
    assert events["variant_count"] == len(_KNOWN_EVENT_TYPES), (
        f"variant_count {events['variant_count']} != live union size "
        f"{len(_KNOWN_EVENT_TYPES)}."
    )


def test_app_openapi_injects_event_union_on_the_live_spec(app) -> None:
    """The wrapped ``app.openapi()`` materializes ``ServerStreamEvent`` live.

    FastAPI does not ``$ref`` the union from the SSE routes, so without the
    wrapper the union is absent from the live ``/openapi.json``. This proves
    the wrapper (the third call site) injects it onto the real spec.
    """
    schemas = app.openapi()["components"]["schemas"]
    assert "ServerStreamEvent" in schemas, (
        "app.openapi() did not inject ServerStreamEvent — the live spec "
        "wrapper regressed; the SDK would not see the event union."
    )
