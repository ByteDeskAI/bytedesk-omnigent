"""The boundary identity value object (:class:`ActingIdentity`).

``ActingIdentity`` fuses the two axes that describe *who is acting*:

- **subject** — the verified inbound :class:`~omnigent.server.principal.Principal`
  (the originating user/tenant), ``None`` in standalone/local mode; and
- **actor** — the running ``agent_id``,

plus an optional on-behalf-of ``delegation`` chain (empty standalone). It is a
plain frozen dataclass with no server/FastAPI imports, so it is safe to carry
across the runner subprocess boundary and reference from the runner hot path.

Construct it via :func:`omnigent.identity.defaults.acting_identity_for` rather
than directly, so the (currently no-op) actor resolution has one home.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.server.principal import Principal


@dataclass(frozen=True)
class ActingIdentity:
    """Who an action is performed *as* and *on behalf of*.

    :param principal: The verified inbound principal (subject). ``None`` means
        "no inbound identity" — standalone/local, the agent acts as its own
        configured service identity.
    :param agent_id: The running agent's id (actor). ``None`` outside an agent
        turn (e.g. tests).
    :param delegation: On-behalf-of chain (outermost first). Empty in standalone
        mode; populated only by a consumer that implements real delegation.
    """

    principal: Principal | None = None
    agent_id: str | None = None
    delegation: tuple[str, ...] = ()
