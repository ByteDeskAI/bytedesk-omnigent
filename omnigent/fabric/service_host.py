"""Internal NATS service host for fabric request/reply endpoints."""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .manifest import DEFAULT_FABRIC_MANIFEST, FabricManifest

ServiceHandler = Callable[[bytes], bytes | Awaitable[bytes]]


@dataclass(frozen=True)
class NatsServiceEndpoint:
    name: str
    subject: str
    handler: ServiceHandler
    queue_group: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class _EndpointStats:
    num_requests: int = 0
    num_errors: int = 0
    processing_time: int = 0
    last_error: str = ""


class NatsServiceHost:
    """Host for `$SRV.PING`, `$SRV.INFO`, `$SRV.STATS`, and endpoint calls."""

    def __init__(
        self,
        *,
        name: str,
        version: str,
        description: str = "",
        metadata: dict[str, str] | None = None,
        service_id: str | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self.metadata = dict(metadata or {})
        self.id = service_id or uuid.uuid4().hex
        self._endpoints: dict[str, NatsServiceEndpoint] = {}
        self._stats: dict[str, _EndpointStats] = {}
        self._started_unix_ns = time.time_ns()

    def add_endpoint(self, endpoint: NatsServiceEndpoint) -> None:
        queue = endpoint.queue_group or f"q.{self.name}"
        endpoint = NatsServiceEndpoint(
            name=endpoint.name,
            subject=endpoint.subject,
            handler=endpoint.handler,
            queue_group=queue,
            metadata=dict(endpoint.metadata),
        )
        self._endpoints[endpoint.subject] = endpoint
        self._stats[endpoint.subject] = _EndpointStats()

    def endpoints(self) -> tuple[NatsServiceEndpoint, ...]:
        return tuple(self._endpoints.values())

    def control_subjects(self) -> tuple[str, ...]:
        return (
            "$SRV.PING",
            f"$SRV.PING.{self.name}",
            f"$SRV.PING.{self.name}.{self.id}",
            "$SRV.INFO",
            f"$SRV.INFO.{self.name}",
            f"$SRV.INFO.{self.name}.{self.id}",
            "$SRV.STATS",
            f"$SRV.STATS.{self.name}",
            f"$SRV.STATS.{self.name}.{self.id}",
        )

    async def handle_endpoint(self, subject: str, payload: bytes) -> bytes:
        endpoint = self._endpoints[subject]
        stats = self._stats[subject]
        started = time.perf_counter_ns()
        stats.num_requests += 1
        try:
            result = endpoint.handler(payload)
            if inspect.isawaitable(result):
                result = await result
            return bytes(result)
        except Exception as exc:
            stats.num_errors += 1
            stats.last_error = str(exc)
            raise
        finally:
            stats.processing_time += time.perf_counter_ns() - started

    async def handle_control(self, subject: str) -> bytes:
        if subject.startswith("$SRV.PING"):
            return self._json(self._ping_response())
        if subject.startswith("$SRV.INFO"):
            return self._json(self._info_response())
        if subject.startswith("$SRV.STATS"):
            return self._json(self._stats_response())
        raise ValueError(f"unsupported service control subject {subject!r}")

    def _base_response(self, response_type: str) -> dict[str, Any]:
        return {
            "type": response_type,
            "name": self.name,
            "id": self.id,
            "version": self.version,
            "metadata": dict(self.metadata),
        }

    def _ping_response(self) -> dict[str, Any]:
        return self._base_response("io.nats.micro.v1.ping_response")

    def _info_response(self) -> dict[str, Any]:
        data = self._base_response("io.nats.micro.v1.info_response")
        data["description"] = self.description
        data["endpoints"] = [
            {
                "name": endpoint.name,
                "subject": endpoint.subject,
                "queue_group": endpoint.queue_group,
                "metadata": dict(endpoint.metadata),
            }
            for endpoint in self._endpoints.values()
        ]
        return data

    def _stats_response(self) -> dict[str, Any]:
        data = self._base_response("io.nats.micro.v1.stats_response")
        data["started"] = self._started_unix_ns
        data["endpoints"] = []
        for subject, endpoint in self._endpoints.items():
            stats = self._stats[subject]
            data["endpoints"].append(
                {
                    "name": endpoint.name,
                    "subject": endpoint.subject,
                    "queue_group": endpoint.queue_group,
                    "num_requests": stats.num_requests,
                    "num_errors": stats.num_errors,
                    "last_error": stats.last_error,
                    "processing_time": stats.processing_time,
                    "average_processing_time": (
                        stats.processing_time // stats.num_requests
                        if stats.num_requests
                        else 0
                    ),
                }
            )
        return data

    @staticmethod
    def _json(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


_SERVICE_DESCRIPTIONS: dict[str, str] = {
    "omnigent.fabric.bootstrap": "Reconcile required NATS assets from the manifest",
    "omnigent.fabric.control": "Fabric readiness, preflight, and discovery",
    "omnigent.fabric.scheduler": "Publish claimed SQL schedule fires as fabric jobs",
    "omnigent.fabric.outbox": "Replay confirmed SQL outbox rows to NATS",
    "omnigent.fabric.placement": "Runner lane, host, and affinity placement",
    "omnigent.fabric.capacity": "Tenant and lane budgets and backpressure",
    "omnigent.fabric.runner_pool": "Warm and persistent runner reconciliation",
    "omnigent.fabric.host_worker": "Host launch, stop, stat, list, and workspace operations",
    "omnigent.fabric.runner_lifecycle": "Runner ready, heartbeat, drain, crash, and stale leases",
    "omnigent.fabric.credential": "Short-lived runner credential mint, revoke, and lookup",
    "omnigent.fabric.artifacts": "Bundle verify, prefetch, pin, and integrity manifests",
    "omnigent.fabric.recovery": "DLQ replay simulation, replay, discard, and stale repair",
    "omnigent.fabric.quarantine": "Host, runner, lane, and credential quarantine policy",
    "omnigent.fabric.timeline": "Session-visible lifecycle event projection",
    "omnigent.fabric.audit": "Immutable fabric audit events",
    "omnigent.fabric.ops": "Operator admin actions that enqueue fabric jobs",
}

_SERVICE_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "omnigent.fabric.bootstrap": (("reconcile", "omnigent.fabric.bootstrap.reconcile"),),
    "omnigent.fabric.control": (
        ("ready", "omnigent.fabric.control.ready"),
        ("preflight", "omnigent.fabric.control.preflight"),
        ("discover", "omnigent.fabric.control.discover"),
    ),
    "omnigent.fabric.scheduler": (("fire", "omnigent.fabric.scheduler.fire"),),
    "omnigent.fabric.outbox": (("replay", "omnigent.fabric.outbox.replay"),),
    "omnigent.fabric.placement": (("place", "omnigent.fabric.placement.place"),),
    "omnigent.fabric.capacity": (("reserve", "omnigent.fabric.capacity.reserve"),),
    "omnigent.fabric.runner_pool": (("reconcile", "omnigent.fabric.runner_pool.reconcile"),),
    "omnigent.fabric.host_worker": (
        ("launch", "omnigent.fabric.host_worker.launch"),
        ("stop", "omnigent.fabric.host_worker.stop"),
        ("stat", "omnigent.fabric.host_worker.stat"),
        ("list", "omnigent.fabric.host_worker.list"),
        ("workspace", "omnigent.fabric.host_worker.workspace"),
    ),
    "omnigent.fabric.runner_lifecycle": (
        ("ready", "omnigent.fabric.runner_lifecycle.ready"),
        ("heartbeat", "omnigent.fabric.runner_lifecycle.heartbeat"),
        ("drain", "omnigent.fabric.runner_lifecycle.drain"),
        ("crash", "omnigent.fabric.runner_lifecycle.crash"),
        ("repair_stale_leases", "omnigent.fabric.runner_lifecycle.repair_stale_leases"),
    ),
    "omnigent.fabric.credential": (
        ("mint", "omnigent.fabric.credential.mint"),
        ("revoke", "omnigent.fabric.credential.revoke"),
        ("lookup", "omnigent.fabric.credential.lookup"),
    ),
    "omnigent.fabric.artifacts": (
        ("verify", "omnigent.fabric.artifacts.verify"),
        ("prefetch", "omnigent.fabric.artifacts.prefetch"),
        ("pin", "omnigent.fabric.artifacts.pin"),
        ("integrity", "omnigent.fabric.artifacts.integrity"),
    ),
    "omnigent.fabric.recovery": (
        ("simulate_replay", "omnigent.fabric.recovery.simulate_replay"),
        ("replay", "omnigent.fabric.recovery.replay"),
        ("discard", "omnigent.fabric.recovery.discard"),
        ("repair_stale", "omnigent.fabric.recovery.repair_stale"),
    ),
    "omnigent.fabric.quarantine": (
        ("apply", "omnigent.fabric.quarantine.apply"),
        ("release", "omnigent.fabric.quarantine.release"),
    ),
    "omnigent.fabric.timeline": (("append", "omnigent.fabric.timeline.append"),),
    "omnigent.fabric.audit": (("append", "omnigent.fabric.audit.append"),),
    "omnigent.fabric.ops": (("enqueue", "omnigent.fabric.ops.enqueue"),),
}


def _service_ack(payload: bytes) -> bytes:
    return json.dumps(
        {"accepted": True, "payload_bytes": len(payload)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def create_required_fabric_service_hosts(
    *,
    manifest: FabricManifest = DEFAULT_FABRIC_MANIFEST,
    version: str = "1.0.0",
) -> dict[str, NatsServiceHost]:
    """Create local service hosts for every required fabric service."""
    hosts: dict[str, NatsServiceHost] = {}
    for service_name in manifest.required_services:
        host = NatsServiceHost(
            name=service_name,
            version=version,
            description=_SERVICE_DESCRIPTIONS.get(service_name, service_name),
            metadata={"manifest_generation": str(manifest.generation)},
        )
        for endpoint_name, subject in _SERVICE_ENDPOINTS.get(
            service_name,
            (("status", f"{service_name}.status"),),
        ):
            host.add_endpoint(
                NatsServiceEndpoint(
                    name=endpoint_name,
                    subject=subject,
                    handler=_service_ack,
                    metadata={"service": service_name},
                )
            )
        hosts[service_name] = host
    return hosts


def required_fabric_service_versions(
    manifest: FabricManifest = DEFAULT_FABRIC_MANIFEST,
) -> dict[str, str]:
    return {
        name: host.version
        for name, host in create_required_fabric_service_hosts(manifest=manifest).items()
    }
