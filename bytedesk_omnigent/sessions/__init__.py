"""Native session-initiate seam (BDP-2279 α3b, ADR-0142).

``sys_session_initiate`` is the **root-session self-re-entry spawn seam**: the
way detached / boot code (the cron scheduler loop, a delivered durable signal)
starts a fresh root agent session. The cron scheduler (BDP-2250) is the durable
*clock*; this is the *dispatch* it was shipped without — ``loop.py``'s
``_log_only_dispatch`` placeholder defers exactly this ("the real dispatch ...
is wired in the scheduler re-home follow-up").

The seam is a Strategy (ADR-0008): a :class:`SessionInitiator` protocol + a
deploy-time registry + a pure :func:`build_cron_dispatch` adapter mapping a fired
:class:`~bytedesk_omnigent.scheduler.scheduler.CronTrigger` onto ``initiate``. The live
initiator (resolve the agent → create the session row via the conversation store
→ bind a runner → post the payload + start the turn) is registered by the server
at deploy; until one is registered the cron loop degrades to log-only, the same
degrade posture the signal bus / cron clock shipped with.
"""

from __future__ import annotations

from bytedesk_omnigent.sessions.initiate import (
    SessionInitiator,
    build_cron_dispatch,
    get_session_initiator,
    set_session_initiator,
)

__all__ = [
    "SessionInitiator",
    "build_cron_dispatch",
    "get_session_initiator",
    "set_session_initiator",
]
