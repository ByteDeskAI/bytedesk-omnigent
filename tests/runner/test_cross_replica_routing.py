"""Cross-replica runner routing tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.coordination.lifecycle import reset_for_tests
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport


class _FakeWebSocket:
    async def send_text(self, data: str) -> None:
        del data

    async def receive_text(self) -> str:
        return ""


class _ConversationStore:
    def __init__(self, conversations: dict[str, Conversation]) -> None:
        self._conversations = conversations

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)


def _hello(*, harnesses: list[str]) -> HelloFrame:
    return HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=harnesses,
        envs=["os_sandbox"],
    )


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
    registry = TunnelRegistry()
    registry.register("runner_one", _FakeWebSocket(), _hello(harnesses=["codex"]))
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_one")})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
    try:
        routed = await router.aclient_for_session_resources("conv_test")
        assert routed.runner_id == "runner_one"
        assert isinstance(routed.client._transport, WSTunnelTransport)  # type: ignore[attr-defined]
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_aclient_for_session_resources_remote_hit_uses_peer_transport() -> None:
    registry = TunnelRegistry()
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_remote")})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]

    backplane = InProcessBackplane("replica-local")
    await backplane.start()
    await backplane.claim_resource("runner", "runner_remote")
    with patch(
        "omnigent.coordination.lifecycle.get_active_backplane",
        return_value=backplane,
    ):
        with patch(
            "omnigent.runner.routing.server_replica_id",
            return_value="replica-other",
        ):
            try:
                routed = await router.aclient_for_session_resources("conv_test")
                assert routed.runner_id == "runner_remote"
                transport = routed.client._transport  # type: ignore[attr-defined]
                assert transport.__class__.__name__ == "PeerTunnelTransport"
            finally:
                await router.aclose()
                await backplane.stop()


@pytest.mark.asyncio
async def test_aclient_for_session_resources_missing_resource_raises() -> None:
    registry = TunnelRegistry()
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_missing")})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
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
    registry = TunnelRegistry()
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_stale")})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
    backplane = InProcessBackplane("replica-local")
    await backplane.start()
    await backplane.claim_resource("runner", "runner_stale")
    with patch(
        "omnigent.coordination.lifecycle.get_active_backplane",
        return_value=backplane,
    ):
        with patch(
            "omnigent.runner.routing.server_replica_id",
            return_value="replica-local",
        ):
            try:
                with pytest.raises(OmnigentError) as excinfo:
                    await router.aclient_for_session_resources("conv_test")
                assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
            finally:
                await router.aclose()
                await backplane.stop()