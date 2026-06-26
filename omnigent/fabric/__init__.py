"""NATS-backed runner fabric contracts and internal adapters."""

from __future__ import annotations

from .credentials import (
    InMemoryRunnerCredentialStore,
    NatsRunnerCredentialStore,
    RunnerLaunchCredential,
    create_runner_credential_store_from_env,
)
from .manifest import DEFAULT_FABRIC_MANIFEST, FabricManifest
from .models import (
    AuditEvent,
    CapacityRecord,
    CredentialReference,
    DlqRecord,
    FabricEnvelope,
    LeaseRecord,
    LifecycleEvent,
    PlacementDecision,
    RunnerHeartbeat,
    RunnerJob,
    SchedulerJob,
    TimelineEvent,
    fabric_schema_hash,
)
from .policies import (
    FabricCapacityRejected,
    InMemoryFabricCapacityPolicy,
    InMemoryFabricRecoveryPolicy,
    InMemoryQuarantinePolicy,
    QuarantineRecord,
    ReplaySimulationRequired,
    WarmFirstPlacementStrategy,
)
from .runner_fabric import (
    FabricRunnerConflict,
    HostRunnerAcquisition,
    HostWorkerRunnerFabric,
    RunnerAcquisitionResult,
)

__all__ = [
    "DEFAULT_FABRIC_MANIFEST",
    "AuditEvent",
    "CapacityRecord",
    "CredentialReference",
    "DlqRecord",
    "FabricCapacityRejected",
    "FabricEnvelope",
    "FabricManifest",
    "FabricRunnerConflict",
    "HostRunnerAcquisition",
    "HostWorkerRunnerFabric",
    "InMemoryFabricCapacityPolicy",
    "InMemoryFabricRecoveryPolicy",
    "InMemoryQuarantinePolicy",
    "InMemoryRunnerCredentialStore",
    "LeaseRecord",
    "LifecycleEvent",
    "NatsRunnerCredentialStore",
    "PlacementDecision",
    "QuarantineRecord",
    "ReplaySimulationRequired",
    "RunnerAcquisitionResult",
    "RunnerHeartbeat",
    "RunnerJob",
    "RunnerLaunchCredential",
    "SchedulerJob",
    "TimelineEvent",
    "WarmFirstPlacementStrategy",
    "create_runner_credential_store_from_env",
    "fabric_schema_hash",
]
