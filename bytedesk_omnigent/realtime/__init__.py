"""ByteDesk realtime bridge — omnigent state/streams → platform SignalR (BDP-2301).

Surface 1: agent roster → org chart. The bridge wraps the concrete agent store
so roster mutations fan out to the platform ``office:agents`` topic (via the
platform Redis that ByteDesk.Realtime reads). Presence (active-when-working) is
the next surface. Installed once from the extension's background lifespan tasks
(after boot re-seed, so seed creates are suppressed).
"""

from bytedesk_omnigent.realtime.bridge import (
    emit_presence,
    emit_roster,
    install as install_realtime_bridge,
)

__all__ = ["install_realtime_bridge", "emit_roster", "emit_presence"]
