"""NATS JetStream Object Store implementation of ArtifactStore (BDP-2380).

Durable, replica-shared blob storage for agent bundles. Replaces the
per-pod ``emptyDir`` ``LocalArtifactStore`` that every ``omnigent-server``
roll wiped (so a durable Postgres agent row pointed at a vanished bundle →
"unable to load agent spec: ag_…"). JetStream Object Store gives us a
single, JetStream-persisted, cross-replica artifact store.

Design patterns
---------------
- **Adapter (ADR-0008).** This class adapts the *async* ``nats.py``
  JetStream Object Store API to the internal *synchronous*
  :class:`~omnigent.stores.artifact_store.ArtifactStore` port. Callers
  invoke ``put``/``get``/``delete``/``exists`` synchronously (the route
  layer / seed run them from worker threads via ``asyncio.to_thread``).
  **All** async/event-loop/thread-bridge machinery is encapsulated here;
  nothing leaks to callers.
- **Strategy + pluggable registry (ADR-0008 / ADR-0145).** This backend is
  an interchangeable strategy selected by URI scheme through the existing
  ``omnigent.stores.factory`` registry — a ``nats://`` location picks it,
  callers are untouched.
- **Idempotency (ADR-0009).** Keys are content-addressed
  (``{agent_id}/{sha256}``), so ``put`` is naturally idempotent: re-putting
  identical content reproduces identical bytes under the same name. We do
  **not** take a single-writer advisory lock around ``put``: concurrent
  puts of *identical* content under a content-addressed key are safe by
  construction (last writer wins with byte-identical data), so the
  lock-free path is correct — stated explicitly so a reviewer sees it was
  considered, not missed.
- **Fail-fast.** A NATS-unreachable condition raises loudly. This store
  never silently degrades to "empty" — silent emptiness is the exact
  failure mode (vanished bundles) this work exists to eliminate.

Async/sync bridge
-----------------
``ArtifactStore`` is synchronous but ``nats.py`` is async, and callers
already run us from worker threads (``asyncio.to_thread``) — so we cannot
borrow the caller's event loop (there is none) nor the server's main loop
(it lives on another thread). The adapter owns a **dedicated asyncio event
loop running in its own daemon background thread**, started lazily on first
use. Each sync method schedules its coroutine onto that loop with
``asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=...)``. The
NATS connection is established lazily on first use and reused, with
infinite reconnect (``max_reconnect_attempts=-1``) mirroring
``omnigent.coordination.nats_backplane``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar
from urllib.parse import urlsplit

from omnigent.stores.artifact_store import ArtifactStore

_T = TypeVar("_T")

_DEFAULT_BUCKET = "omnigent-artifacts"

# Generous per-call ceiling: a multi-MB bundle chunk-uploads over the
# JetStream Object Store, and a reconnect storm must not wedge a caller
# forever. The bridge raises TimeoutError past this, surfacing loudly.
_CALL_TIMEOUT_S = 120.0

# Connector returns (connection, object_not_found_error_type). The
# error type is injectable so tests can supply a fake; production binds
# it to nats.js.object_store.ObjectNotFoundError.
Connector = Callable[[], Awaitable[tuple[Any, type[BaseException]]]]


class NatsObjectStoreArtifactStore(ArtifactStore):
    """ArtifactStore backed by a single JetStream Object Store bucket.

    ``storage_location`` is a ``nats://<server>[:<port>][/<bucket>]`` URL.
    The bucket defaults to ``omnigent-artifacts`` when the URL has no path.
    """

    def __init__(
        self,
        storage_location: str,
        *,
        connector: Connector | None = None,
    ) -> None:
        """Initialize the adapter.

        Construction is cheap and does **not** connect — connection is lazy
        (first ``put``/``get``/``delete``/``exists``), so building the store
        is safe when NATS is not yet reachable.

        :param storage_location: ``nats://<server>[/<bucket>]`` URL.
        :param connector: Test seam. An async callable returning
            ``(connection, ObjectNotFoundError_type)``. Defaults to a real
            ``nats.connect`` against the parsed server URL.
        """
        super().__init__(storage_location)
        split = urlsplit(storage_location)
        self._nats_url = f"{split.scheme}://{split.netloc}"
        bucket = split.path.lstrip("/")
        self._bucket = bucket or _DEFAULT_BUCKET
        self._connector = connector or self._default_connector

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

        # Connection state — only touched on the dedicated loop thread.
        self._nc: Any = None
        self._obj: Any = None
        self._not_found: type[BaseException] = KeyError

    # ── public read-only accessors (used by factory + tests) ─────

    @property
    def nats_url(self) -> str:
        """The NATS server URL (scheme://host:port), bucket stripped."""
        return self._nats_url

    @property
    def bucket(self) -> str:
        """The JetStream Object Store bucket name."""
        return self._bucket

    # ── ArtifactStore interface (sync facade over async backend) ─

    def put(self, key: str, data: bytes) -> None:
        """Store *data* under *key* (idempotent for content-addressed keys)."""
        self._run(self._put(key, data))

    def get(self, key: str) -> bytes:
        """Retrieve the blob for *key*.

        :raises KeyError: If no object exists for *key*.
        """
        return self._run(self._get(key))

    def delete(self, key: str) -> None:
        """Remove the blob for *key*. No-op if absent."""
        self._run(self._delete(key))

    def exists(self, key: str) -> bool:
        """:returns: ``True`` if an object exists for *key*."""
        return self._run(self._exists(key))

    # ── async backend (runs on the dedicated loop thread) ────────

    async def _put(self, key: str, data: bytes) -> None:
        obj = await self._object_store()
        await obj.put(_object_name(key), data)

    async def _get(self, key: str) -> bytes:
        obj = await self._object_store()
        try:
            result = await obj.get(_object_name(key))
        except self._not_found as exc:  # ObjectNotFoundError → KeyError
            raise KeyError(key) from exc
        data: bytes = bytes(result.data)
        return data

    async def _delete(self, key: str) -> None:
        obj = await self._object_store()
        try:
            await obj.delete(_object_name(key))
        except self._not_found:
            return  # already absent — delete is a no-op

    async def _exists(self, key: str) -> bool:
        obj = await self._object_store()
        try:
            await obj.get(_object_name(key))
        except self._not_found:
            return False
        return True

    async def _object_store(self) -> Any:
        """Lazily connect + bind the bucket, reusing across calls.

        A connection/bind failure propagates (fail-fast) — never swallowed
        into an empty-store fallback.
        """
        if self._obj is not None:
            return self._obj
        if self._nc is None:
            self._nc, self._not_found = await self._connector()
        js = self._nc.jetstream()
        try:
            obj = await js.object_store(self._bucket)
        except Exception:  # noqa: BLE001 — bucket may not exist yet; create it
            obj = await js.create_object_store(self._bucket)
        self._obj = obj
        return obj

    async def _default_connector(self) -> tuple[Any, type[BaseException]]:
        """Open a real NATS connection (production path).

        :raises RuntimeError: If ``nats-py`` is not installed.
        """
        try:
            import nats
            import nats.js.object_store as _object_store
        except ImportError as exc:
            raise RuntimeError(
                "nats-py is required for the nats:// artifact store; "
                "install omnigent[coordination]"
            ) from exc
        nc = await nats.connect(
            servers=[self._nats_url],
            name="omnigent-artifact-store",
            max_reconnect_attempts=-1,
        )
        # nats-py re-exports ObjectNotFoundError from the module but doesn't
        # declare it in __all__, so mypy can't see it on the foreign SDK.
        not_found: type[BaseException] = _object_store.ObjectNotFoundError  # type: ignore[attr-defined]
        return nc, not_found

    # ── dedicated-loop machinery ─────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start (once) the dedicated event loop on a daemon thread."""
        if self._loop is not None:
            return self._loop
        with self._lifecycle_lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run_loop() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(
                target=_run_loop,
                name=f"nats-artifact-store-{id(self):x}",
                daemon=True,
            )
            thread.start()
            ready.wait()
            self._loop = loop
            self._thread = thread
            return loop

    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Bridge a coroutine onto the dedicated loop and block for the result.

        Uses ``run_coroutine_threadsafe`` so a caller on any thread (incl.
        a worker thread with no event loop, the real invocation pattern via
        ``asyncio.to_thread``) gets a synchronous result. Exceptions —
        including a NATS-unreachable connect failure — propagate to the
        caller; this store never silently degrades to empty.
        """
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=_CALL_TIMEOUT_S)


def _object_name(key: str) -> str:
    """Map an ArtifactStore key to a JetStream Object Store object name.

    JetStream object names accept ``[-/_=.A-Za-z0-9]`` (``VALID_KEY_RE``),
    which already covers content-addressed bundle keys
    (``{agent_id}/{sha256}`` — only ``[a-z0-9_]`` + ``/``). So the mapping
    is identity: no escaping is needed and the round-trip is exact.

    :param key: Forward-slash-separated artifact key.
    :returns: The object name (== *key*).
    :raises ValueError: If *key* is empty.
    """
    if not key:
        raise ValueError("invalid artifact key: empty")
    return key
