"""Strategy dispatch for session events forwarded to runners."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx

from omnigent.entities import Conversation, ErrorData
from omnigent.runner.routing import RunnerRouter
from omnigent.server.schemas import SessionEventInput
from omnigent.stores import ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.file_store import FileStore

from .._helpers import _SessionEventDispatchResult


class NativeTerminalEnsureOutcome(Protocol):
    """Structural view of a native-terminal readiness result."""

    error: ErrorData | None
    policy_notice: str | None


class PersistNativeTerminalFailure(Protocol):
    """Persist the AP-owned failure turn for a native terminal message."""

    def __call__(
        self,
        session_id: str,
        conv: Conversation,
        body: SessionEventInput,
        conversation_store: ConversationStore,
        error: ErrorData,
        runner_router: RunnerRouter | None,
        *,
        created_by: str | None,
    ) -> Awaitable[str]: ...


class ForwardNativeTerminalMessage(Protocol):
    """Forward a web-chat message into a native terminal harness."""

    def __call__(
        self,
        runner_client: httpx.AsyncClient,
        session_id: str,
        conv: Conversation,
        body: SessionEventInput,
        *,
        file_store: FileStore | None,
        artifact_store: ArtifactStore | None,
    ) -> Awaitable[None]: ...


class ForwardRunnerEvent(Protocol):
    """Persist and forward a regular session event to a runner."""

    def __call__(
        self,
        session_id: str,
        conv: Conversation,
        body: SessionEventInput,
        conversation_store: ConversationStore,
        runner_client: httpx.AsyncClient,
        *,
        agent_name: str | None,
        file_store: FileStore | None,
        artifact_store: ArtifactStore | None,
        has_mcp_servers: bool,
        created_by: str | None,
    ) -> Awaitable[str]: ...


class EnsureNativeTerminalReady(Protocol):
    """Check that a native terminal is available for message injection."""

    def __call__(
        self,
        runner_client: httpx.AsyncClient,
        session_id: str,
        conv: Conversation,
    ) -> Awaitable[NativeTerminalEnsureOutcome]: ...


class PersistNativePolicyNotice(Protocol):
    """Persist a non-fatal native terminal policy notice."""

    def __call__(
        self,
        session_id: str,
        conversation_store: ConversationStore,
        reason: str,
    ) -> Awaitable[None]: ...


class PendingInputRecorder(Protocol):
    """Record an optimistic native-terminal input bubble."""

    def __call__(
        self,
        conversation_id: str,
        content: list[dict[str, Any]],
        created_by: str | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class SessionEventDispatchContext:
    """Inputs required to dispatch one client session event to a runner."""

    session_id: str
    conversation: Conversation
    body: SessionEventInput
    conversation_store: ConversationStore
    runner_client: httpx.AsyncClient
    agent_name: str | None
    file_store: FileStore | None
    artifact_store: ArtifactStore | None
    has_mcp_servers: bool = False
    created_by: str | None = None
    runner_router: RunnerRouter | None = None


class SessionEventDispatchStrategy(Protocol):
    """One dispatch algorithm for a session event."""

    def can_dispatch(self, context: SessionEventDispatchContext) -> bool:
        """Return whether this strategy owns the dispatch."""
        ...

    async def dispatch(
        self,
        context: SessionEventDispatchContext,
    ) -> _SessionEventDispatchResult:
        """Dispatch the event and return the route-facing result."""
        ...


@dataclass(frozen=True)
class NativeTerminalMessageDispatchStrategy:
    """Dispatch web-chat messages into native terminal sessions."""

    is_native_terminal_session: Callable[[Conversation], bool]
    build_native_terminal_message_event: Callable[
        [Conversation, SessionEventInput],
        dict[str, Any],
    ]
    ensure_native_terminal_ready: EnsureNativeTerminalReady
    persist_native_terminal_failure: PersistNativeTerminalFailure
    persist_native_policy_notice: PersistNativePolicyNotice
    record_pending_input: PendingInputRecorder
    resolve_pending_input: Callable[[str, str], None]
    forward_native_terminal_message: ForwardNativeTerminalMessage

    def can_dispatch(self, context: SessionEventDispatchContext) -> bool:
        """Return true for native-terminal user message events."""
        return context.body.type == "message" and self.is_native_terminal_session(
            context.conversation
        )

    async def dispatch(
        self,
        context: SessionEventDispatchContext,
    ) -> _SessionEventDispatchResult:
        """Forward a native-terminal message without AP-side persistence."""
        self.build_native_terminal_message_event(context.conversation, context.body)
        ensure_outcome: NativeTerminalEnsureOutcome = await self.ensure_native_terminal_ready(
            context.runner_client,
            context.session_id,
            context.conversation,
        )
        if ensure_outcome.error is not None:
            item_id = await self.persist_native_terminal_failure(
                context.session_id,
                context.conversation,
                context.body,
                context.conversation_store,
                ensure_outcome.error,
                context.runner_router,
                created_by=context.created_by,
            )
            return _SessionEventDispatchResult(item_id=item_id, pending_id=None)
        if ensure_outcome.policy_notice is not None:
            await self.persist_native_policy_notice(
                context.session_id,
                context.conversation_store,
                ensure_outcome.policy_notice,
            )

        content = context.body.data.get("content")
        pending_id: str | None = (
            self.record_pending_input(
                context.session_id,
                cast("list[dict[str, Any]]", content),
                created_by=context.created_by,
            )
            if isinstance(content, list) and content
            else None
        )
        forwarded = False
        try:
            await self.forward_native_terminal_message(
                context.runner_client,
                context.session_id,
                context.conversation,
                context.body,
                file_store=context.file_store,
                artifact_store=context.artifact_store,
            )
            forwarded = True
        finally:
            if not forwarded and pending_id is not None:
                self.resolve_pending_input(context.session_id, pending_id)
        return _SessionEventDispatchResult(item_id=None, pending_id=pending_id)


@dataclass(frozen=True)
class DefaultRunnerEventDispatchStrategy:
    """Dispatch all non-native events through the standard runner forward."""

    forward_event: ForwardRunnerEvent

    def can_dispatch(self, context: SessionEventDispatchContext) -> bool:
        """Return true as the catch-all dispatch strategy."""
        del context
        return True

    async def dispatch(
        self,
        context: SessionEventDispatchContext,
    ) -> _SessionEventDispatchResult:
        """Persist and forward a regular event."""
        item_id = await self.forward_event(
            context.session_id,
            context.conversation,
            context.body,
            context.conversation_store,
            context.runner_client,
            agent_name=context.agent_name,
            file_store=context.file_store,
            artifact_store=context.artifact_store,
            has_mcp_servers=context.has_mcp_servers,
            created_by=context.created_by,
        )
        return _SessionEventDispatchResult(item_id=item_id, pending_id=None)


@dataclass(frozen=True)
class SessionEventDispatcher:
    """Select and run a strategy for one session event."""

    strategies: Sequence[SessionEventDispatchStrategy]

    async def dispatch(
        self,
        context: SessionEventDispatchContext,
    ) -> _SessionEventDispatchResult:
        """Dispatch through the first matching strategy."""
        for strategy in self.strategies:
            if strategy.can_dispatch(context):
                return await strategy.dispatch(context)
        raise RuntimeError("no session event dispatch strategy matched")
