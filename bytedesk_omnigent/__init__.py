"""ByteDesk first-party omnigent extension package (ADR-0143, BDP-2291).

All ByteDesk functionality lives here — OUT of the upstream-tracked ``omnigent/``
core — and is mounted through the generic ``omnigent.extensions`` entry-point seam.
Phase 1 is a passthrough + proof route; later phases compose the moved feature
submodules (goals, governance, bus, ingress, cron, peer, deliberation, outcomes,
release, policies, tools) onto :class:`~bytedesk_omnigent.extension.BytedeskExtension`.
"""

from __future__ import annotations

from bytedesk_omnigent.extension import BytedeskExtension

__all__ = ["BytedeskExtension"]
