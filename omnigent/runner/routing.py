"""Conversation-aware runner routing for the Omnigent server.

The runner control registry is the source of truth for launched runners on
this replica. When coordination is enabled, :meth:`RunnerRouter.aclient_*`
can detect stale cross-replica ownership via
:class:`CoordinationBackplane.resolve_resource`, but runner HTTP dispatch
still goes through the configured runner transport factory.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from omnigent.coordination.protocol import ResourceKind
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.runner.transports.factory import (
    RunnerTransportFactory,
    resolve_runner_transport_factory,
)
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec import AgentSpec

if TYPE_CHECKING:
    from omnigent.stores import ConversationStore


_EXECUTOR_TYPE_TO_HARNESS: dict[str, str] = {"claude_sdk": "claude-sdk"}


class RunnerSession(Protocol):
    hello: Any


class RunnerRegistry(Protocol):
    def get(self, runner_id: str) -> RunnerSession | None: ...

    def runner_owner(self, runner_id: str) -> str | None: ...

    def launch_owner(self, runner_id: str) -> str | None: ...

    def launch_token(self, runner_id: str) -> str | None: ...


def runner_dispatch_harness(spec: AgentSpec) -> str | None:
    """
    Return the runner-routed harness for an agent spec, if any.

    Mirrors the harness selection in
    :func:`omnigent.runtime.workflow._create_executor`: direct
    executors return ``None`` unless they explicitly name a harness.

    :param spec: Parsed agent spec from the agent cache.
    :returns: Harness key, e.g. ``"codex"``, when the executor is
        runner-routed; otherwise ``None``.
    """
    executor_type = spec.executor.type
    harness = spec.executor.config.get("harness")
    if not harness:
        harness = _EXECUTOR_TYPE_TO_HARNESS.get(executor_type, executor_type)
    canonical = canonicalize_harness(harness) or harness
    return canonical if canonical in _HARNESS_MODULES else None


@dataclass(frozen=True)
class RoutedRunner:
    """
    Runner selected for a conversation dispatch.

    :param runner_id: Runner UUID, e.g.
        ``"runner_0123456789abcdef"``.
    :param client: ``httpx.AsyncClient`` that routes requests to
        ``runner_id`` through the configured control-plane transport.
    """

    runner_id: str
    client: httpx.AsyncClient


class RunnerRouter:
    """
    Select runners from the control registry.

    :param registry: Registry that records trusted runner launch ownership
        and optional live local session state.
    :param conversation_store: Store used to read
        ``conversations.runner_id`` affinity.
    """

    def __init__(
        self,
        *,
        registry: RunnerRegistry,
        conversation_store: ConversationStore,
        transport_factory: RunnerTransportFactory | None = None,
        credential_store: Any | None = None,
    ) -> None:
        self._registry = registry
        self._conversation_store = conversation_store
        self._credential_store = credential_store
        self._transport_factory = transport_factory or resolve_runner_transport_factory(
            auth_token_resolver=self._launch_token,
        )
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = threading.RLock()

    def client_for_conversation(self, *, conversation_id: str, harness: str) -> RoutedRunner:
        """
        Return the runner client for a harness-backed conversation turn.

        Local-registry only — use :meth:`aclient_for_conversation` in
        async server code for cross-replica forwarding.

        :param conversation_id: Conversation id, e.g.
            ``"conv_0123456789abcdef"``.
        :param harness: Harness kind requested by the agent spec,
            e.g. ``"codex"``.
        :returns: Selected runner id and client.
        :raises OmnigentError: If the conversation has no runner
            binding, the bound runner is offline, or the runner
            cannot serve the requested harness.
        """
        conv = self._conversation_store.get_conversation(conversation_id)
        if conv is None:
            raise OmnigentError("conversation not found", code=ErrorCode.NOT_FOUND)
        if conv.runner_id:
            return self._routed_pinned_runner_local(conv.runner_id, harness=harness)
        raise OmnigentError(
            f"conversation {conversation_id!r} is not bound to a runner; "
            "resume the session to bind a registered runner",
            code=ErrorCode.CONFLICT,
        )

    async def aclient_for_conversation(
        self,
        *,
        conversation_id: str,
        harness: str,
    ) -> RoutedRunner:
        """Async variant with coordination-backed cross-replica routing."""
        conv = self._conversation_store.get_conversation(conversation_id)
        if conv is None:
            raise OmnigentError("conversation not found", code=ErrorCode.NOT_FOUND)
        if conv.runner_id:
            return await self._route_runner(conv.runner_id, harness=harness)
        raise OmnigentError(
            f"conversation {conversation_id!r} is not bound to a runner; "
            "resume the session to bind a registered runner",
            code=ErrorCode.CONFLICT,
        )

    def client_for_session_resources(self, conversation_id: str) -> RoutedRunner:
        """
        Return a runner client for session resource access (local only).

        Use :meth:`aclient_for_session_resources` in async server code.
        """
        conv = self._conversation_store.get_conversation(conversation_id)
        if conv is None:
            raise OmnigentError("conversation not found", code=ErrorCode.NOT_FOUND)
        if conv.runner_id:
            return self._routed_runner_local(conv.runner_id, conversation_id)
        raise OmnigentError(
            f"conversation {conversation_id!r} is not bound to a runner; "
            "resume the session to bind a registered runner",
            code=ErrorCode.CONFLICT,
        )

    async def aclient_for_session_resources(self, conversation_id: str) -> RoutedRunner:
        """Async variant with coordination-backed cross-replica routing."""
        conv = self._conversation_store.get_conversation(conversation_id)
        if conv is None:
            raise OmnigentError("conversation not found", code=ErrorCode.NOT_FOUND)
        if conv.runner_id:
            return await self._route_runner(conv.runner_id)
        raise OmnigentError(
            f"conversation {conversation_id!r} is not bound to a runner; "
            "resume the session to bind a registered runner",
            code=ErrorCode.CONFLICT,
        )

    def client_for_existing_conversation(self, conversation_id: str) -> RoutedRunner | None:
        """Return the pinned runner client (local registry only)."""
        conv = self._conversation_store.get_conversation(conversation_id)
        if conv is None or not conv.runner_id:
            return None
        return self._routed_runner_local(conv.runner_id, conversation_id)

    async def aclient_for_existing_conversation(
        self,
        conversation_id: str,
    ) -> RoutedRunner | None:
        """Async variant with coordination-backed cross-replica routing."""
        conv = self._conversation_store.get_conversation(conversation_id)
        if conv is None or not conv.runner_id:
            return None
        return await self._route_runner(conv.runner_id)

    def runner_is_online(self, runner_id: str) -> bool:
        """
        Return whether *runner_id* is currently connected locally.

        :param runner_id: Runner UUID, e.g.
            ``"runner_0123456789abcdef"``.
        :returns: ``True`` when the registry has a live session.
        """
        return (
            self._registry.get(runner_id) is not None
            or self._launch_token(runner_id) is not None
        )

    def runner_owner(self, runner_id: str) -> str | None:
        """
        Return the authenticated owner of *runner_id*, or ``None``.

        Delegates to the control registry. Returns ``None`` when the
        runner has no live-session or launch-owner record.

        :param runner_id: Runner UUID, e.g.
            ``"runner_0123456789abcdef"``.
        :returns: Owner user id, or ``None``.
        """
        return self._registry.runner_owner(runner_id) or self._launch_owner(runner_id)

    async def aclose(self) -> None:
        """
        Close cached runner clients.

        :returns: None.
        """
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.aclose()

    async def _resolve_owner(self, kind: ResourceKind, resource_id: str) -> str | None:
        from omnigent.coordination.lifecycle import get_active_backplane

        backplane = get_active_backplane()
        if backplane is None:
            return None
        return await backplane.resolve_resource(kind, resource_id)

    def _launch_token(self, runner_id: str) -> str | None:
        token_lookup = getattr(self._registry, "launch_token", None)
        if not callable(token_lookup):
            return None
        return token_lookup(runner_id)

    def _launch_owner(self, runner_id: str) -> str | None:
        owner_lookup = getattr(self._registry, "launch_owner", None)
        if not callable(owner_lookup):
            return None
        return owner_lookup(runner_id)

    async def _route_runner(
        self,
        runner_id: str,
        *,
        harness: str | None = None,
    ) -> RoutedRunner:
        session = self._registry.get(runner_id)
        if session is not None:
            if harness is not None and not _runner_supports_harness(session, harness):
                raise OmnigentError(
                    f"runner {runner_id!r} does not support harness {harness!r}",
                    code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
                )
            return RoutedRunner(
                runner_id=runner_id,
                client=self._client_for_runner(runner_id),
            )
        if self._launch_token(runner_id) is not None:
            return RoutedRunner(
                runner_id=runner_id,
                client=self._client_for_runner(runner_id),
            )
        if await self._hydrate_launch_credential(runner_id):
            return RoutedRunner(
                runner_id=runner_id,
                client=self._client_for_runner(runner_id),
            )

        owner = await self._resolve_owner("runner", runner_id)
        if owner is None:
            raise OmnigentError(
                f"runner {runner_id!r} is offline; resume the session to bind a registered runner",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        raise OmnigentError(
            f"runner {runner_id!r} is owned by replica {owner!r} but has no NATS launch token",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )

    async def _hydrate_launch_credential(self, runner_id: str) -> bool:
        lookup = getattr(self._credential_store, "lookup_launch_token", None)
        if not callable(lookup):
            return False
        credential = await lookup(runner_id)
        if credential is None:
            return False
        record = getattr(self._registry, "record_launch_owner", None)
        if callable(record):
            record(credential.runner_id, credential.owner, token=credential.token)
        return True

    def _routed_runner_local(self, runner_id: str, conversation_id: str) -> RoutedRunner:
        session = self._registry.get(runner_id)
        if session is None and self._launch_token(runner_id) is None:
            raise OmnigentError(
                f"runner {runner_id!r} is offline for conversation {conversation_id!r}",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        return RoutedRunner(
            runner_id=runner_id,
            client=self._client_for_runner(runner_id),
        )

    def _routed_pinned_runner_local(self, runner_id: str, *, harness: str) -> RoutedRunner:
        session = self._registry.get(runner_id)
        if session is None and self._launch_token(runner_id) is None:
            raise OmnigentError(
                f"runner {runner_id!r} is offline; resume the session to bind a registered runner",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        if session is not None and not _runner_supports_harness(session, harness):
            raise OmnigentError(
                f"runner {runner_id!r} does not support harness {harness!r}",
                code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
            )
        return RoutedRunner(runner_id=runner_id, client=self._client_for_runner(runner_id))

    def _client_for_runner(self, runner_id: str) -> httpx.AsyncClient:
        """
        Return a cached control-plane client for *runner_id*.

        :param runner_id: Runner UUID, e.g.
            ``"runner_0123456789abcdef"``.
        :returns: ``httpx.AsyncClient`` using
            the configured runner control-plane transport.
        """
        with self._lock:
            client = self._clients.get(runner_id)
            if client is None:
                client = httpx.AsyncClient(
                    transport=self._transport_factory(runner_id),
                    base_url="http://runner",
                    timeout=httpx.Timeout(5.0, read=None),
                )
                self._clients[runner_id] = client
            return client

def _runner_supports_harness(session: RunnerSession, harness: str) -> bool:
    """
    Return whether a runner advertised support for *harness*.

    :param session: Optional live runner session.
    :param harness: Harness kind requested by the agent spec,
        e.g. ``"claude-sdk"``.
    :returns: ``True`` when the runner hello frame includes the
        harness kind.
    """
    canonical = canonicalize_harness(harness) or harness
    return canonical in session.hello.harnesses or harness in session.hello.harnesses
