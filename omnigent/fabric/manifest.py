"""Checked NATS asset manifest for the runner fabric."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_LANE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass(frozen=True)
class StreamAsset:
    name: str
    subjects: tuple[str, ...]
    retention: str = "limits"
    storage: str = "file"
    max_age_seconds: int | None = None
    workqueue: bool = False

    def to_config(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "subjects": list(self.subjects),
            "retention": "workqueue" if self.workqueue else self.retention,
            "storage": self.storage,
        }
        if self.max_age_seconds is not None:
            data["max_age"] = self.max_age_seconds
        return data


@dataclass(frozen=True)
class KvBucketAsset:
    name: str
    description: str
    ttl_seconds: int | None = None

    def to_config(self) -> dict[str, Any]:
        data: dict[str, Any] = {"bucket": self.name, "description": self.description}
        if self.ttl_seconds is not None:
            data["ttl"] = self.ttl_seconds
        return data


@dataclass(frozen=True)
class ObjectStoreAsset:
    name: str
    description: str

    def to_config(self) -> dict[str, Any]:
        return {"bucket": self.name, "description": self.description}


@dataclass(frozen=True)
class FabricManifest:
    generation: int
    streams: tuple[StreamAsset, ...]
    kv_buckets: tuple[KvBucketAsset, ...]
    object_stores: tuple[ObjectStoreAsset, ...]
    required_services: tuple[str, ...]
    lanes: tuple[str, ...] = ("default", "priority", "maintenance")

    def lane_subject(self, lane: str) -> str:
        if not _LANE_RE.match(lane):
            raise ValueError(f"invalid fabric lane {lane!r}")
        return f"omnigent.runner.jobs.{lane}"

    def to_topology(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "streams": [stream.name for stream in self.streams],
            "kv_buckets": [bucket.name for bucket in self.kv_buckets],
            "object_stores": [store.name for store in self.object_stores],
            "required_services": list(self.required_services),
            "lanes": [
                {"lane": lane, "subject": self.lane_subject(lane)} for lane in self.lanes
            ],
        }


DEFAULT_FABRIC_MANIFEST = FabricManifest(
    generation=1,
    streams=(
        StreamAsset(
            name="OMNIGENT_SCHEDULER_JOBS",
            subjects=("omnigent.scheduler.jobs",),
            workqueue=True,
        ),
        StreamAsset(
            name="OMNIGENT_RUNNER_JOBS",
            subjects=(
                "omnigent.runner.jobs.default",
                "omnigent.runner.jobs.priority",
                "omnigent.runner.jobs.maintenance",
            ),
            workqueue=True,
        ),
        StreamAsset(
            name="OMNIGENT_RUNNER_EVENTS",
            subjects=("omnigent.runner.events.>",),
        ),
        StreamAsset(
            name="OMNIGENT_RUNNER_DLQ",
            subjects=("omnigent.runner.dlq.>",),
        ),
        StreamAsset(
            name="OMNIGENT_FABRIC_AUDIT",
            subjects=("omnigent.fabric.audit.>",),
        ),
        StreamAsset(
            name="OMNIGENT_AGENT_EVENTS",
            subjects=("omnigent.agent_store.>",),
        ),
    ),
    kv_buckets=(
        KvBucketAsset("OMNIGENT_AGENT_HEADS", "AgentStore current records and history"),
        KvBucketAsset("OMNIGENT_AGENT_NAME_INDEX", "AgentStore template name index"),
        KvBucketAsset("OMNIGENT_AGENT_SESSION_INDEX", "AgentStore session index"),
        KvBucketAsset("omnigent-fabric-lane-config", "Fabric lane configuration"),
        KvBucketAsset("omnigent-fabric-host-state", "Host state and drain markers"),
        KvBucketAsset("omnigent-fabric-runner-registry", "Runner registry records"),
        KvBucketAsset("omnigent-fabric-runner-credentials", "Runner credential refs"),
        KvBucketAsset("omnigent-fabric-leases", "Fabric lease records"),
        KvBucketAsset("omnigent-fabric-job-results", "Fabric job result pointers"),
        KvBucketAsset("omnigent-fabric-capacity", "Capacity and circuit state"),
        KvBucketAsset("omnigent-fabric-warm-pool-state", "Warm runner pool state"),
        KvBucketAsset("omnigent-fabric-schema-hashes", "Canonical schema hashes"),
        KvBucketAsset("omnigent-fabric-deployment-generation", "Deployment generation"),
        KvBucketAsset("omnigent-fabric-quarantine-state", "Quarantine state"),
    ),
    object_stores=(
        ObjectStoreAsset("omnigent-fabric-artifact-manifests", "Artifact manifests"),
        ObjectStoreAsset("omnigent-fabric-schema-snapshots", "Schema snapshots"),
        ObjectStoreAsset("omnigent-fabric-snapshots", "Fabric snapshots"),
        ObjectStoreAsset("omnigent-fabric-runner-crash-bundles", "Runner crash bundles"),
        ObjectStoreAsset("omnigent-fabric-replay-packs", "DLQ replay packs"),
        ObjectStoreAsset("omnigent-fabric-integrity-manifests", "Integrity manifests"),
        ObjectStoreAsset("omnigent-agent-revisions", "AgentStore revision snapshots"),
    ),
    required_services=(
        "omnigent.fabric.bootstrap",
        "omnigent.fabric.control",
        "omnigent.fabric.scheduler",
        "omnigent.fabric.outbox",
        "omnigent.fabric.placement",
        "omnigent.fabric.capacity",
        "omnigent.fabric.runner_pool",
        "omnigent.fabric.host_worker",
        "omnigent.fabric.runner_lifecycle",
        "omnigent.fabric.credential",
        "omnigent.fabric.artifacts",
        "omnigent.fabric.recovery",
        "omnigent.fabric.quarantine",
        "omnigent.fabric.timeline",
        "omnigent.fabric.audit",
        "omnigent.fabric.ops",
    ),
)
