"""Remote adapters: the engine calls a connected app's sensors/actuators (Phase 4).

ADR-0008 Adapter: a :class:`RemoteSensor` / :class:`RemoteActuator` makes a remote
HTTP endpoint look like the in-process :class:`~bytedesk_omnigent.engine.sensors.Sensor`
/ :class:`~bytedesk_omnigent.engine.providers.contract.Actuator` the engine already
consumes — so a registered provider's sensors are usable by the resolver and its
actuators by any actuator caller, with zero engine-side knowledge of the domain.

URLs (POST):
  sensor   → ``{base_url}/goal-sensors/{name}/evaluate``   body ``{query}``
  actuator → ``{base_url}/goal-actuators/{name}/execute``  body ``{action}``

Reverse-auth: ``manifest.auth.header: manifest.auth.secret`` is sent on every call.

The sensor is **sync** (the resolver calls ``evaluate`` synchronously) and takes an
injected ``post`` callable so tests mock httpx without a network. The actuator is
**async** (its Protocol is async) and takes an injected async ``post``. Both default
to a thin httpx call.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from bytedesk_omnigent.engine.providers.contract import ActuatorResult
from bytedesk_omnigent.engine.providers.registry import ProviderManifest
from bytedesk_omnigent.engine.sensors import SensorContext, SensorReading

# Injectable transports — (url, json_body, headers) -> response dict.
SyncPost = Callable[[str, dict, dict], dict]
AsyncPost = Callable[[str, dict, dict], Awaitable[dict]]


def _auth_headers(manifest: ProviderManifest) -> dict[str, str]:
    if manifest.auth and manifest.auth.secret:
        return {manifest.auth.header: manifest.auth.secret}
    return {}


def _unwrap_api_response(body: dict) -> dict:
    """Accept either raw contract JSON or ByteDesk's ApiResponse<T> envelope."""
    data = body.get("data")
    return data if isinstance(data, dict) else body


def _pick(data: dict, camel: str, snake: str, default: Any = None) -> Any:
    if camel in data:
        return data[camel]
    if snake in data:
        return data[snake]
    return default


def _default_sync_post(url: str, body: dict, headers: dict) -> dict:
    import httpx

    resp = httpx.post(url, json=body, headers=headers, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


async def _default_async_post(url: str, body: dict, headers: dict) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


class RemoteSensor:
    """A :class:`Sensor` backed by a connected app's HTTP endpoint."""

    def __init__(
        self, name: str, manifest: ProviderManifest, *, post: SyncPost | None = None
    ) -> None:
        self.name = name
        self._manifest = manifest
        self._post = post or _default_sync_post

    def evaluate(self, query: dict[str, Any], ctx: SensorContext) -> SensorReading:
        url = f"{self._manifest.base_url}/goal-sensors/{self.name}/evaluate"
        body = self._post(url, {"query": query, "now": ctx.now}, _auth_headers(self._manifest))
        body = _unwrap_api_response(body)
        # Map the app's response onto the canonical reading shape; missing fields
        # fail closed (not satisfied) so a malformed remote reply never over-fires.
        return {
            "satisfied": bool(body.get("satisfied", False)),
            "value": body.get("value"),
            "observed_at": int(_pick(body, "observedAt", "observed_at", ctx.now)),
            "stale_after_s": _pick(body, "staleAfterS", "stale_after_s"),
        }


class RemoteActuator:
    """An :class:`Actuator` backed by a connected app's HTTP endpoint."""

    def __init__(
        self,
        name: str,
        manifest: ProviderManifest,
        *,
        risk_tier: int | str = "medium",
        post: AsyncPost | None = None,
    ) -> None:
        self.name = name
        self.risk_tier = risk_tier
        self._manifest = manifest
        self._post = post or _default_async_post

    async def execute(self, action: dict[str, Any]) -> ActuatorResult:
        url = f"{self._manifest.base_url}/goal-actuators/{self.name}/execute"
        body = await self._post(url, {"action": action}, _auth_headers(self._manifest))
        body = _unwrap_api_response(body)
        return ActuatorResult(
            ok=bool(body.get("ok", body.get("success", False))),
            output=body.get("output", body.get("resultRef")),
            detail=body.get("detail"),
        )


def register_remote_providers(
    provider_registry: Any,
    *,
    sensor_registry: Any,
    actuator_registry: Any,
    sync_post: SyncPost | None = None,
    async_post: AsyncPost | None = None,
) -> None:
    """Register every manifest's sensors/actuators as Remote adapters.

    Each manifest sensor becomes a :class:`RemoteSensor` in ``sensor_registry`` and
    each actuator a :class:`RemoteActuator` in ``actuator_registry`` — so the engine
    consumes a connected app's capabilities through the same registries as the
    built-ins. Already-registered names are skipped (idempotent re-discovery).
    """
    for manifest in provider_registry.providers():
        for sensor_name in manifest.sensors:
            if sensor_name in sensor_registry.names():
                continue
            sensor_registry.register(
                sensor_name,
                lambda m=manifest, n=sensor_name: RemoteSensor(n, m, post=sync_post),
            )
        for spec in manifest.actuators:
            if spec.name in actuator_registry.names():
                continue
            actuator_registry.register(
                spec.name,
                lambda m=manifest, s=spec: RemoteActuator(
                    s.name, m, risk_tier=s.risk_tier, post=async_post
                ),
            )


__all__ = [
    "RemoteActuator",
    "RemoteSensor",
    "register_remote_providers",
]
