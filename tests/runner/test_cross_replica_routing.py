"""Cross-replica runner routing tests."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.coordination.lifecycle import reset_for_tests
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.fabric.credentials import RunnerLaunchCredential
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.nats_transport import NatsRunnerTransport


@dataclass(frozen=True)
class _Hello:
    harnesses: list[str]


@dataclass(frozen=True)
class _RunnerSession:
    hello: _Hello


class _RunnerRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, _RunnerSession] = {}
        self._owners: dict[str, str] = {}
        self._tokens: dict[str, str] = {}

    def register(self, runner_id: str, hello: _Hello) -> None:
        self._sessions[runner_id] = _RunnerSession(hello=hello)

    def get(self, runner_id: str) -> _RunnerSession | None:
        return self._sessions.get(runner_id)

    def runner_owner(self, runner_id: str) -> str | None:
        return self._owners.get(runner_id)

    def record_launch_owner(
        self,
        runner_id: str,
        owner: str,
        *,
        token: str | None = None,
    ) -> None:
        self._owners[runner_id] = owner
        if token is not None:
            self._tokens[runner_id] = token

    def launch_owner(self, runner_id: str) -> str | None:
        return self._owners.get(runner_id)

    def launch_token(self, runner_id: str) -> str | None:
        return self._tokens.get(runner_id)


class _ConversationStore:
    def __init__(self, conversations: dict[str, Conversation]) -> None:
        self._conversations = conversations

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)


class _CredentialStore:
    def __init__(self, credentials: dict[str, RunnerLaunchCredential]) -> None:
        self._credentials = credentials

    async def lookup_launch_token(self, runner_id: str) -> RunnerLaunchCredential | None:
        return self._credentials.get(runner_id)


def _hello(*, harnesses: list[str]) -> _Hello:
    return _Hello(harnesses=harnesses)


@pytest.fixture(autouse=True)
def _reset_coordination() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


def _conversation(
    conversation_id: str = "conv_test",
    *,
    runner_id: str | None = None,
) -> Conversation:
    return Conversation(
        id=conversation_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=conversation_id,
        runner_id=runner_id,
    )


@pytest.mark.asyncio
async def test_aclient_for_session_resources_local_hit_no_peer_client() -> None:
    registry = _RunnerRegistry()
    registry.register("runner_one", _hello(harnesses=["codex"]))
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_one")})
    router = RunnerRouter(registry=registry, conversation_store=store)
    try:
        routed = await router.aclient_for_session_resources("conv_test")
        assert routed.runner_id == "runner_one"
        assert isinstance(routed.client._transport, NatsRunnerTransport)  # type: ignore[attr-defined]
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_aclient_for_session_resources_launch_record_uses_direct_nats() -> None:
    registry = _RunnerRegistry()
    registry.record_launch_owner("runner_remote", "alice", token="launch-token")
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_remote")})
    router = RunnerRouter(registry=registry, conversation_store=store)

    try:
        routed = await router.aclient_for_session_resources("conv_test")
        assert routed.runner_id == "runner_remote"
        transport = routed.client._transport  # type: ignore[attr-defined]
        assert isinstance(transport, NatsRunnerTransport)
        assert transport._auth_token == "launch-token"  # type: ignore[attr-defined]
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_aclient_for_session_resources_hydrates_shared_launch_credential() -> None:
    registry = _RunnerRegistry()
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_remote")})
    credential_store = _CredentialStore(
        {
            "runner_remote": RunnerLaunchCredential(
                runner_id="runner_remote",
                owner="alice",
                token="shared-launch-token",
                expires_unix_ms=9_999,
            )
        }
    )
    router = RunnerRouter(
        registry=registry,
        conversation_store=store,
        credential_store=credential_store,
    )

    try:
        routed = await router.aclient_for_session_resources("conv_test")
        assert routed.runner_id == "runner_remote"
        assert registry.launch_token("runner_remote") == "shared-launch-token"
        transport = routed.client._transport  # type: ignore[attr-defined]
        assert isinstance(transport, NatsRunnerTransport)
        assert transport._auth_token == "shared-launch-token"  # type: ignore[attr-defined]
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_aclient_for_session_resources_remote_claim_without_launch_token_raises() -> None:
    registry = _RunnerRegistry()
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_remote")})
    router = RunnerRouter(registry=registry, conversation_store=store)

    backplane = InProcessBackplane("replica-remote")
    await backplane.start()
    await backplane.claim_resource("runner", "runner_remote")
    with patch(
        "omnigent.coordination.lifecycle.get_active_backplane",
        return_value=backplane,
    ):
        try:
            with pytest.raises(OmnigentError) as excinfo:
                await router.aclient_for_session_resources("conv_test")
            assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
            assert "has no NATS launch token" in str(excinfo.value)
        finally:
            await router.aclose()
            await backplane.stop()


@pytest.mark.asyncio
async def test_aclient_for_session_resources_missing_resource_raises() -> None:
    registry = _RunnerRegistry()
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_missing")})
    router = RunnerRouter(registry=registry, conversation_store=store)
    backplane = InProcessBackplane("replica-local")
    await backplane.start()
    with patch(
        "omnigent.coordination.lifecycle.get_active_backplane",
        return_value=backplane,
    ):
        try:
            with pytest.raises(OmnigentError) as excinfo:
                await router.aclient_for_session_resources("conv_test")
            assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
        finally:
            await router.aclose()
            await backplane.stop()


@pytest.mark.asyncio
async def test_aclient_for_session_resources_self_claim_without_local_tunnel_raises() -> None:
    registry = _RunnerRegistry()
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_stale")})
    router = RunnerRouter(registry=registry, conversation_store=store)
    backplane = InProcessBackplane("replica-local")
    await backplane.start()
    await backplane.claim_resource("runner", "runner_stale")
    with patch(
        "omnigent.coordination.lifecycle.get_active_backplane",
        return_value=backplane,
    ):
        try:
            with pytest.raises(OmnigentError) as excinfo:
                await router.aclient_for_session_resources("conv_test")
            assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
        finally:
            await router.aclose()
            await backplane.stop()
