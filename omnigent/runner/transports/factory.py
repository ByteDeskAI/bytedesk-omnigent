"""Runner transport resolution."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from omnigent.runner.transports.nats_transport import NatsRunnerTransport

RunnerTransportFactory = Callable[[str], httpx.AsyncBaseTransport]
RunnerAuthTokenResolver = Callable[[str], str | None]


def nats_runner_transport_factory(
    runner_id: str,
    *,
    auth_token: str | None = None,
) -> httpx.AsyncBaseTransport:
    return NatsRunnerTransport(runner_id, auth_token=auth_token)


def resolve_runner_transport_factory(
    auth_token_resolver: RunnerAuthTokenResolver | None = None,
) -> RunnerTransportFactory:
    """Resolve the direct replacement runner transport.

    NATS is intentionally the only default implementation. Tests can inject a
    factory into :class:`omnigent.runner.routing.RunnerRouter`; production must
    configure NATS instead of falling back to the old WS tunnel.
    """

    def _factory(runner_id: str) -> httpx.AsyncBaseTransport:
        return nats_runner_transport_factory(
            runner_id,
            auth_token=auth_token_resolver(runner_id) if auth_token_resolver else None,
        )

    return _factory


__all__ = [
    "RunnerAuthTokenResolver",
    "RunnerTransportFactory",
    "nats_runner_transport_factory",
    "resolve_runner_transport_factory",
]
