"""Runner control-plane registry for NATS-launched runners."""

from __future__ import annotations

import threading

_MAX_LAUNCH_OWNERS = 8192


class RunnerControlRegistry:
    """Track trusted launch ownership for runner control-plane dispatch.

    This registry intentionally has no WebSocket session lifecycle. It stores
    the server-minted launch token and owner for a runner id so the server can
    authorize runner callbacks and route HTTP over the NATS transport.
    """

    def __init__(self, *, max_launch_owners: int = _MAX_LAUNCH_OWNERS) -> None:
        if max_launch_owners < 1:
            raise ValueError("max_launch_owners must be at least 1")
        self._max_launch_owners = max_launch_owners
        self._launch_owners: dict[str, str] = {}
        self._launch_tokens: dict[str, str] = {}
        self._lock = threading.RLock()

    def get(self, runner_id: str) -> None:
        """Return no live local session; runner traffic is NATS-based."""
        del runner_id

    def online_runner_ids(self) -> list[str]:
        """Return runner ids with server-issued launch records."""
        with self._lock:
            return list(self._launch_owners)

    def runner_owner(self, runner_id: str) -> str | None:
        """Return no live-session owner; use ``launch_owner`` instead."""
        del runner_id
        return None

    def record_launch_owner(
        self,
        runner_id: str,
        owner: str,
        *,
        token: str | None = None,
    ) -> None:
        """Record the trusted owner and optional token for a launched runner."""
        with self._lock:
            self._launch_owners.pop(runner_id, None)
            self._launch_owners[runner_id] = owner
            if token is not None:
                self._launch_tokens[runner_id] = token
            while len(self._launch_owners) > self._max_launch_owners:
                oldest = next(iter(self._launch_owners))
                self._launch_owners.pop(oldest, None)
                self._launch_tokens.pop(oldest, None)

    def launch_owner(self, runner_id: str) -> str | None:
        """Return the trusted launch owner for a runner id."""
        with self._lock:
            return self._launch_owners.get(runner_id)

    def launch_token(self, runner_id: str) -> str | None:
        """Return the launch token for a runner id, when recorded."""
        with self._lock:
            return self._launch_tokens.get(runner_id)


__all__ = ["RunnerControlRegistry"]
