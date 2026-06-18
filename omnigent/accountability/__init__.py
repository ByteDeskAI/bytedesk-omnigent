"""Native accountability loop (BDP-2272 C4, ADR-0142).

The org's accountability organ: a periodic sweep over the goals backlog (C3,
``omnigent/goals.py``) + peer bus (C2, ``omnigent/peer.py``) that closes the
why-act loop —

- **rebalance**: an owned goal idle past a stall threshold is reopened for
  re-claim and its dropped owner is notified (a goal can't rot in one agent's
  queue);
- **escalate**: a ``blocked`` goal is surfaced to a manager via a peer
  ``escalation`` message (a blocked goal demands a human/manager decision).

Pure tick (store-driven + injectable) so it is unit-provable standalone; the
server ``_lifespan`` loop is layered on top, a direct sibling of the cron
scheduler (BDP-2250) and signal-bus reaper (BDP-2248). The spawn-breadth governor
(C5, ``policies/builtins/spawn_governor.py``) is the other half of BDP-2272.
"""

from __future__ import annotations

from omnigent.accountability.loop import (
    AccountabilityReport,
    accountability_loop,
    run_accountability_tick,
)

__all__ = [
    "AccountabilityReport",
    "accountability_loop",
    "run_accountability_tick",
]
