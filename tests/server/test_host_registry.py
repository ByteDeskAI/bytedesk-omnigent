"""Tests for the in-memory host connection registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostRegistry, RunnerExitReports


@dataclass
class FakeWebSocket:
    """Minimal WebSocket fake for registry tests.

    Records sent text frames so tests can assert on outbound
    messages without a real network.

    :param sent: List of text frames sent via ``send_text``.
    """

    sent: list[str] = field(default_factory=list)

    async def send_text(self, data: str) -> None:
        """Record a sent frame.

        :param data: The text frame content.
        """
        self.sent.append(data)

    async def receive_text(self) -> str:
        """Block forever — tests don't read from the fake.

        :returns: Never returns in practice.
        """
        await asyncio.sleep(3600)
        return ""  # pragma: no cover


def _make_hello(name: str = "test-host") -> HostHelloFrame:
    """Build a minimal HostHelloFrame for tests.

    :param name: Human-readable host name.
    :returns: A :class:`HostHelloFrame` with default values.
    """
    return HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name=name,
    )


def test_register_and_get() -> None:
    """
    Verify that a registered host can be retrieved by ID.

    If get() returns None after register(), the registry's
    internal dict is not being populated.
    """
    registry = HostRegistry()
    ws = FakeWebSocket()
    conn = registry.register("host_aaa", ws, _make_hello(), owner="alice")

    fetched = registry.get("host_aaa")
    assert fetched is conn
    assert fetched is not None
    assert fetched.host_id == "host_aaa"
    assert fetched.owner == "alice"
    assert fetched.hello.name == "test-host"


def test_deregister() -> None:
    """
    Verify that deregister removes the host from the registry.

    If get() still returns the connection after deregister(), the
    pop() call is missing or targeting the wrong key.
    """
    registry = HostRegistry()
    conn = registry.register("host_bbb", FakeWebSocket(), _make_hello(), owner="bob")
    assert registry.deregister(conn) is True

    assert registry.get("host_bbb") is None


def test_deregister_is_idempotent_noop_when_not_current() -> None:
    """
    Verify deregister never raises and is a no-op when the conn isn't the
    current registration (already removed).

    The disconnect callback may fire for a conn that was superseded /
    already torn down; it must not raise and must return ``False`` (BDP-2540).
    """
    registry = HostRegistry()
    conn = registry.register("host_z", FakeWebSocket(), _make_hello(), owner="zoe")
    assert registry.deregister(conn) is True
    assert registry.deregister(conn) is False


def test_online_host_ids() -> None:
    """
    Verify that online_host_ids returns all registered hosts
    and updates after deregister.

    If a deregistered host still appears, the dict pop is broken.
    If a registered host is missing, the dict insert is broken.
    """
    registry = HostRegistry()
    conn_c1 = registry.register("host_c1", FakeWebSocket(), _make_hello(), owner="carol")
    registry.register("host_c2", FakeWebSocket(), _make_hello(), owner="carol")

    ids = registry.online_host_ids()
    assert set(ids) == {"host_c1", "host_c2"}

    registry.deregister(conn_c1)
    ids = registry.online_host_ids()
    assert ids == ["host_c2"]


def test_register_replaces_stale_connection() -> None:
    """
    Verify that registering the same host_id replaces the old
    connection (newest wins) and poisons the old outbound queue.

    If the old connection isn't replaced, a reconnecting host would
    have two live entries, causing frame routing confusion.
    """
    registry = HostRegistry()
    old_ws = FakeWebSocket()
    old_conn = registry.register("host_ddd", old_ws, _make_hello(), owner="dave")

    new_ws = FakeWebSocket()
    new_conn = registry.register("host_ddd", new_ws, _make_hello(), owner="dave")

    # New connection is the one returned by get().
    assert registry.get("host_ddd") is new_conn
    assert new_conn is not old_conn

    # Old connection's outbound queue was poisoned with None.
    # The None sentinel tells the sender loop to exit.
    poison = old_conn.outbound_queue.get_nowait()
    assert poison is None


def test_deregister_is_conn_guarded_against_coroll_race() -> None:
    """BDP-2540: an OLD conn's teardown must not deregister the NEW conn that
    replaced it.

    The recurring "runner didn't come online" wedge: on a host co-roll the new
    pod registers (newest-wins), then the old pod's tunnel teardown called
    ``deregister(host_id)`` and blindly popped the new live conn — orphaning a
    connected, dispatchable host out of the registry until it happened to
    reconnect. deregister is now conn-guarded (mirrors ``evict``/``send_text``).
    """
    registry = HostRegistry()
    old_conn = registry.register("host_co", FakeWebSocket(), _make_hello(), owner="o")
    new_conn = registry.register("host_co", FakeWebSocket(), _make_hello(), owner="o")
    assert registry.get("host_co") is new_conn

    # Old conn's teardown deregisters — must be a no-op; the live host stays.
    assert registry.deregister(old_conn) is False
    assert registry.get("host_co") is new_conn

    # The current conn's own teardown does remove it.
    assert registry.deregister(new_conn) is True
    assert registry.get("host_co") is None


def test_evict_retires_current_connection_and_poisons_queue() -> None:
    """BDP-2491: evict() drops the registration and poisons the queue.

    A wedged-but-registered host is retired by evict() exactly like a
    newest-wins replacement: get() goes None and the outbound queue is
    poisoned with the None sentinel so the sender loop exits and trips the
    tunnel teardown (which reconnects the host). evict() returns True.
    """
    registry = HostRegistry()
    conn = registry.register("host_evict", FakeWebSocket(), _make_hello(), owner="erin")

    assert registry.evict(conn) is True
    assert registry.get("host_evict") is None
    assert conn.outbound_queue.get_nowait() is None


def test_evict_is_noop_when_connection_already_replaced() -> None:
    """BDP-2491: evict() is current-conn-guarded — it never retires a fresh tunnel.

    If the host already reconnected (a newer conn replaced the wedged one),
    evicting the stale conn returns False and leaves the new registration and
    its (unpoisoned) outbound queue intact.
    """
    registry = HostRegistry()
    old_conn = registry.register("host_race", FakeWebSocket(), _make_hello(), owner="ray")
    new_conn = registry.register("host_race", FakeWebSocket(), _make_hello(), owner="ray")

    assert registry.evict(old_conn) is False
    assert registry.get("host_race") is new_conn
    # The live connection's queue must be untouched (no spurious poison).
    assert new_conn.outbound_queue.empty()


def test_send_text_enqueues_frame() -> None:
    """
    Verify that send_text puts the frame on the connection's
    outbound queue.

    If the frame doesn't appear in the queue, the sender loop
    would never transmit it and the host would never receive the
    launch request.
    """
    registry = HostRegistry()
    ws = FakeWebSocket()
    conn = registry.register("host_eee", ws, _make_hello(), owner="eve")

    registry.send_text(conn, '{"kind": "host.launch_runner"}')

    # Frame should be on the outbound queue.
    frame = conn.outbound_queue.get_nowait()
    assert frame == '{"kind": "host.launch_runner"}'


def test_send_text_raises_if_connection_replaced() -> None:
    """
    Verify that send_text raises ConnectionError if the connection
    was replaced by a newer one.

    Without this guard, a stale reference could enqueue frames on
    a dead connection's queue, which would never be drained.
    """
    registry = HostRegistry()
    old_conn = registry.register("host_fff", FakeWebSocket(), _make_hello(), owner="frank")
    registry.register("host_fff", FakeWebSocket(), _make_hello(), owner="frank")

    with pytest.raises(ConnectionError, match="connection was replaced"):
        registry.send_text(old_conn, '{"kind": "test"}')


def test_get_returns_none_for_unknown() -> None:
    """
    Verify that get() returns None for a host that was never
    registered.
    """
    registry = HostRegistry()
    assert registry.get("host_nonexistent") is None


# ── RunnerExitReports ───────────────────────────────────


def test_exit_reports_record_and_get_visible_for_owner() -> None:
    """
    A recorded exit report is readable by the owner with its exact
    error text.

    The error message is the entire diagnostic value of the report —
    if it comes back mangled or None, the waiting client falls back
    to the blind 60s timeout this feature removes.
    """
    reports = RunnerExitReports()
    reports.record("runner_abc", "runner process exited with code 1", "alice")

    assert reports.get_visible("runner_abc", "alice") == ("runner process exited with code 1")


def test_exit_reports_hidden_from_other_users() -> None:
    """
    Another user's report reads as None (W6-2 posture).

    The report's log tail can contain agent output, so a non-owner
    must see nothing — the same "reveal nothing about other users'
    runners" rule the status endpoint applies to ``online``.
    """
    reports = RunnerExitReports()
    reports.record("runner_abc", "runner process exited with code 1", "alice")

    assert reports.get_visible("runner_abc", "bob") is None


def test_exit_reports_visible_when_auth_disabled() -> None:
    """
    With auth disabled on both sides (owner None, user None) the
    report is readable — single-user/local mode must not lose the
    diagnostic.
    """
    reports = RunnerExitReports()
    reports.record("runner_abc", "runner process exited with code 1", None)

    assert reports.get_visible("runner_abc", None) == ("runner process exited with code 1")


def test_exit_reports_missing_runner_returns_none() -> None:
    """
    An unknown runner id reads as None — the status endpoint then
    omits the ``error`` field rather than inventing one.
    """
    reports = RunnerExitReports()
    assert reports.get_visible("runner_never_reported", "alice") is None


def test_exit_reports_get_is_unscoped() -> None:
    """
    The unscoped ``get`` returns the error regardless of owner.

    Used by the session snapshot, which has already authorized access
    by session permission — the report is that session's own runner, so
    no owner re-check is needed (unlike the auth-less status endpoint,
    which must use ``get_visible``). If ``get`` ever started scoping by
    owner, the snapshot would silently drop the error for shared
    sessions viewed by a non-owner.
    """
    reports = RunnerExitReports()
    reports.record("runner_abc", "runner process exited with code 1", "alice")

    assert reports.get("runner_abc") == "runner process exited with code 1"
    # Missing runner still reads None (snapshot then leaves last_task_error unset).
    assert reports.get("runner_unknown") is None
