"""Goal Engine — the keystone that turns a ready goal into agent work (BDP-2583).

A goal is dead data until something opens a session and an agent works it. This
package is that bridge:

- :mod:`dispatcher` — the pure-ish ``dispatch_goal`` (spawn one live session per
  ``(goal, period)``, idempotently).
- :mod:`cron` — ``goal_cron_dispatch``: a fired cron trigger whose payload is a
  goal routes to ``dispatch_goal`` (recurring / until_done cadence).
- :mod:`loop` — the advisory-locked tick that dispatches ready *immediate* goals
  that have no live session yet.
"""

from __future__ import annotations

from bytedesk_omnigent.engine.dispatcher import DispatchResult, dispatch_goal

__all__ = ["DispatchResult", "dispatch_goal"]
