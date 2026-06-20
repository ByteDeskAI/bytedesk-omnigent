"""Resolve the omnigent-server replica identity."""

from __future__ import annotations

import os
import socket
import uuid


def server_replica_id() -> str:
    """Return a stable-ish replica id for this server process.

    Prefers ``OMNIGENT_REPLICA_ID``, then Kubernetes downward API hostname
    (``HOSTNAME``), then hostname + short random suffix.
    """
    explicit = os.getenv("OMNIGENT_REPLICA_ID", "").strip()
    if explicit:
        return explicit
    host = os.getenv("HOSTNAME", "").strip() or socket.gethostname()
    if host and host != "localhost":
        return host
    return f"local-{uuid.uuid4().hex[:12]}"