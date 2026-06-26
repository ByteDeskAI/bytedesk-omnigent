"""Self-heal + host-failover orchestration tests (BDP-2579 rungs 1-3, F4/F5).

These exercise the heal *decision* logic (single-flight, liveness gate, CAS
repin, managed-vs-plain branch, rung-2 failover, failover.enabled kill-switch,
rung-3 graceful) by stubbing the deep launch primitives — which carry their own
tests — so the orchestration is tested in isolation.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes import sessions as S
from omnigent.stores.host_store import Host

pytestmark = pytest.mark.asyncio


def _conv(
    *,
    runner_id: str = "runner_dead",
    host_id: str = "host_a",
    harness: str | None = "claude-sdk",
) -> Conversation:
    return Conversation(
        id="conv_1",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_1",
        runner_id=runner_id,
        host_id=host_id,
        workspace="/ws",
        harness_override=harness,
    )


def _host(host_id: str, *, sandbox_provider: str | None = None) -> Host:
    return Host(
        host_id=host_id,
        name=host_id,
        owner="alice@example.com",
        status="online",
        created_at=0,
        updated_at=10_000_000_000,  # far-future so host_is_live() is True
        sandbox_provider=sandbox_provider,
        configured_harnesses={"claude-sdk": True},
    )


class _ConvStore:
    def __init__(self, conv: Conversation) -> None:
        self.conv = conv
        self.cas_runner_calls: list[tuple[str, str]] = []
        self.cas_host_calls: list[tuple[str, str, str, str]] = []

    def get_conversation(self, _sid: str) -> Conversation:
        return self.conv

    def owner_for_runner(self, _rid: str) -> str:
        return "alice@example.com"

    def cas_runner_id(self, _cid: str, expected: str, new: str) -> bool:
        self.cas_runner_calls.append((expected, new))
        self.conv.runner_id = new  # simulate the swap
        return True

    def cas_host_and_runner(
        self, _cid: str, eh: str, er: str, nh: str, nr: str
    ) -> bool:
        self.cas_host_calls.append((eh, er, nh, nr))
        self.conv.host_id = nh
        self.conv.runner_id = nr
        return True


class _HostStore:
    def __init__(self, hosts: dict[str, Host], listed: list[Host] | None = None) -> None:
        self._hosts = hosts
        self._listed = listed if listed is not None else list(hosts.values())

    def get_host(self, host_id: str) -> Host | None:
        return self._hosts.get(host_id)

    def list_hosts(self, _owner: str | None) -> list[Host]:
        return list(self._listed)


class _HostRegistry:
    def get(self, _host_id: str) -> Any:
        return None  # force the cross-replica _launch_runner_on_host_id path

    def evict(self, _conn: Any) -> bool:
        return True


def _request(state: dict[str, Any]):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _base_state(conv_store: _ConvStore, host_store: _HostStore) -> dict[str, Any]:
    return {
        "conversation_store": conv_store,
        "runner_router": object(),
        "host_registry": _HostRegistry(),
        "host_store": host_store,
        "runner_control_registry": None,
        "runner_exit_reports": None,
        "sandbox_config": None,
        "managed_launches": None,
    }


@pytest.fixture(autouse=True)
def _stub_publishers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"pending": [], "recovered": []}
    monkeypatch.setattr(
        S, "_publish_terminal_pending", lambda sid, p: calls["pending"].append((sid, p))
    )
    monkeypatch.setattr(
        S, "_publish_runner_recovered_status", lambda sid: calls["recovered"].append(sid)
    )
    return calls


async def test_rung1_relaunch_on_live_host_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv_store = _ConvStore(_conv())
    host_store = _HostStore({"host_a": _host("host_a")})
    state = _base_state(conv_store, host_store)

    # Dead runner at the liveness gate; recovers after the (one) relaunch.
    monkeypatch.setattr(S, "_get_runner_client", _async_return(None))
    launches: list[str] = []

    async def _fake_launch_on_id(
        conv, cs, hr, host_id, *, owner=None, runner_control_registry=None, repin=None
    ):
        launches.append(host_id)
        assert repin is not None and repin("runner_new") is True  # CAS exercised
        return S._HostLaunchAttempt(runner_id="runner_new", acked=True, repinned=True)

    monkeypatch.setattr(S, "_launch_runner_on_host_id", _fake_launch_on_id)
    monkeypatch.setattr(S, "_wait_for_runner_client", _async_return(object()))

    req = _request(state)
    results = await asyncio.gather(
        S._heal_session_runner("conv_1", req),
        S._heal_session_runner("conv_1", req),
    )
    assert results == [True, True]
    assert launches == ["host_a"]  # single-flight: exactly one relaunch
    assert conv_store.cas_runner_calls == [("runner_dead", "runner_new")]


async def test_healthy_runner_is_not_healed(monkeypatch: pytest.MonkeyPatch) -> None:
    conv_store = _ConvStore(_conv())
    host_store = _HostStore({"host_a": _host("host_a")})
    state = _base_state(conv_store, host_store)

    monkeypatch.setattr(S, "_get_runner_client", _async_return(object()))
    monkeypatch.setattr(S, "_runner_client_ready", _async_return(True))

    def _boom(*_a, **_k):  # any launch is a bug
        raise AssertionError("must not relaunch a healthy runner")

    monkeypatch.setattr(S, "_launch_runner_on_host_id", _boom)

    assert await S._heal_session_runner("conv_1", _request(state)) is True


async def test_managed_session_relaunches_sandbox_never_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv_store = _ConvStore(_conv(host_id="host_managed"))
    host_store = _HostStore({"host_managed": _host("host_managed", sandbox_provider="modal")})
    state = _base_state(conv_store, host_store)

    monkeypatch.setattr(S, "_get_runner_client", _async_return(None))
    monkeypatch.setattr(S, "_maybe_relaunch_managed_sandbox", _async_return(True))

    def _no_failover(*_a, **_k):
        raise AssertionError("managed sessions must never cross-host failover")

    monkeypatch.setattr(S, "_launch_runner_on_host_id", _no_failover)
    monkeypatch.setattr(S, "_failover_to_new_host", _no_failover)

    assert await S._heal_session_runner("conv_1", _request(state)) is True


async def test_plain_host_wedge_fails_over_to_capable_owner_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv_store = _ConvStore(_conv())
    host_store = _HostStore(
        {"host_a": _host("host_a"), "host_b": _host("host_b")},
        listed=[_host("host_a"), _host("host_b")],
    )
    state = _base_state(conv_store, host_store)
    monkeypatch.setattr(S, "_get_runner_client", _async_return(None))

    async def _fake_launch_on_id(
        conv, cs, hr, host_id, *, owner=None, runner_control_registry=None, repin=None
    ):
        if host_id == "host_a":
            # rung-1: bound host is wedged (registered tunnel never ACKs).
            assert repin is not None and repin("runner_r1") is True
            return S._HostLaunchAttempt(runner_id="runner_r1", acked=False, repinned=True)
        # rung-2: failover target launches cleanly.
        assert repin is not None and repin("runner_b") is True
        return S._HostLaunchAttempt(runner_id="runner_b", acked=True, repinned=True)

    monkeypatch.setattr(S, "_launch_runner_on_host_id", _fake_launch_on_id)
    monkeypatch.setattr(S, "_wait_for_runner_client", _async_return(object()))

    assert await S._heal_session_runner("conv_1", _request(state)) is True
    # (host_id, runner_id) repinned together to the new host.
    assert conv_store.cas_host_calls == [("host_a", "runner_r1", "host_b", "runner_b")]


async def test_failover_disabled_falls_through_to_rung3(
    monkeypatch: pytest.MonkeyPatch,
    _stub_publishers: dict[str, list[Any]],
) -> None:
    monkeypatch.setenv("OMNIGENT_FAILOVER_ENABLED", "false")
    conv_store = _ConvStore(_conv())
    host_store = _HostStore({"host_a": _host("host_a"), "host_b": _host("host_b")})
    state = _base_state(conv_store, host_store)
    monkeypatch.setattr(S, "_get_runner_client", _async_return(None))

    async def _wedged(
        conv, cs, hr, host_id, *, owner=None, runner_control_registry=None, repin=None
    ):
        assert host_id == "host_a", "must never try a second host when failover off"
        assert repin is not None and repin("runner_r1") is True
        return S._HostLaunchAttempt(runner_id="runner_r1", acked=False, repinned=True)

    monkeypatch.setattr(S, "_launch_runner_on_host_id", _wedged)
    monkeypatch.setattr(S, "_wait_for_runner_client", _async_return(None))

    healed = await S._heal_session_runner("conv_1", _request(state))
    assert healed is False
    assert conv_store.cas_host_calls == []  # no cross-host failover
    # Rung-3 graceful: reconnecting state, never a 503 storm.
    assert _stub_publishers["pending"] == [("conv_1", True)]


def _async_return(value: Any):
    async def _f(*_a, **_k):
        return value

    return _f
