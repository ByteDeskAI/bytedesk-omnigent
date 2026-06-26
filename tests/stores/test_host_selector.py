"""Unit tests for the failover HostSelector strategy (BDP-2579 F4)."""

from __future__ import annotations

from omnigent.stores.host_store import Host, LiveHostSelector


def _host(
    host_id: str,
    *,
    status: str = "online",
    updated_at: int = 1000,
    sandbox_provider: str | None = None,
    harnesses: dict[str, bool] | None = None,
) -> Host:
    return Host(
        host_id=host_id,
        name=f"name-{host_id}",
        owner="alice@example.com",
        status=status,
        created_at=0,
        updated_at=updated_at,
        sandbox_provider=sandbox_provider,
        configured_harnesses=harnesses,
    )


def test_selects_first_live_plain_capable_host() -> None:
    sel = LiveHostSelector()
    candidates = [
        _host("h_managed", sandbox_provider="modal", harnesses={"claude-sdk": True}),
        _host("h_good", harnesses={"claude-sdk": True}),
        _host("h_also_good", harnesses={"claude-sdk": True}),
    ]
    chosen = sel.select(
        candidates, harness="claude-sdk", exclude_host_ids=set(), now=1000
    )
    assert chosen is not None and chosen.host_id == "h_good"  # first non-managed live


def test_excludes_failed_and_cooldown_offline_wrongharness_managed() -> None:
    sel = LiveHostSelector()
    candidates = [
        _host("h_failed", harnesses={"claude-sdk": True}),  # excluded set
        _host("h_cooldown", harnesses={"claude-sdk": True}),  # excluded set
        _host("h_offline", status="offline", harnesses={"claude-sdk": True}),
        _host("h_stale", updated_at=1, harnesses={"claude-sdk": True}),  # not fresh
        _host("h_managed", sandbox_provider="modal", harnesses={"claude-sdk": True}),
        _host("h_wrongharness", harnesses={"codex": True}),  # not capable
        _host("h_unknownharness", harnesses=None),  # never reported → not capable
        _host("h_winner", harnesses={"claude-sdk": True}),
    ]
    chosen = sel.select(
        candidates,
        harness="claude-sdk",
        exclude_host_ids={"h_failed", "h_cooldown"},
        now=1000,
    )
    assert chosen is not None and chosen.host_id == "h_winner"


def test_returns_none_when_no_host_qualifies() -> None:
    sel = LiveHostSelector()
    candidates = [_host("h_managed", sandbox_provider="modal")]
    assert (
        sel.select(candidates, harness="claude-sdk", exclude_host_ids=set(), now=1000)
        is None
    )


def test_harness_none_skips_capability_filter() -> None:
    sel = LiveHostSelector()
    candidates = [_host("h_any", harnesses=None)]
    chosen = sel.select(candidates, harness=None, exclude_host_ids=set(), now=1000)
    assert chosen is not None and chosen.host_id == "h_any"
