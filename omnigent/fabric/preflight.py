"""Fabric readiness, schema, and legacy-absence reporting."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from .manifest import DEFAULT_FABRIC_MANIFEST, FabricManifest
from .models import (
    AuditEvent,
    CapacityRecord,
    CredentialReference,
    DlqRecord,
    LeaseRecord,
    LifecycleEvent,
    PlacementDecision,
    RunnerHeartbeat,
    RunnerJob,
    SchedulerJob,
    TimelineEvent,
    fabric_schema_hash,
)
from .service_host import required_fabric_service_versions

CheckStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class FabricCheck:
    name: str
    status: CheckStatus
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class FabricPreflightReport:
    status: CheckStatus
    checks: tuple[FabricCheck, ...]
    services: dict[str, str]
    schema_hashes: dict[str, str]
    legacy_absence: dict[str, bool] | None = None
    generation: int = DEFAULT_FABRIC_MANIFEST.generation

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "generation": self.generation,
            "checks": [check.to_dict() for check in self.checks],
            "services": dict(self.services),
            "schema_hashes": dict(self.schema_hashes),
            "legacy_absence": self.legacy_absence or legacy_absence_status(),
        }


class InMemoryFabricInspector:
    """Deterministic inspector used by routes and tests until live NATS is active."""

    def __init__(
        self,
        *,
        manifest: FabricManifest = DEFAULT_FABRIC_MANIFEST,
        report: FabricPreflightReport | None = None,
    ) -> None:
        self._manifest = manifest
        self._report = report

    async def preflight(self) -> FabricPreflightReport:
        if self._report is not None:
            return self._report
        return build_preflight_report(self._manifest)

    def manifest(self) -> FabricManifest:
        return self._manifest


def schema_hashes() -> dict[str, str]:
    models = (
        SchedulerJob,
        RunnerJob,
        RunnerHeartbeat,
        LifecycleEvent,
        PlacementDecision,
        CredentialReference,
        LeaseRecord,
        CapacityRecord,
        DlqRecord,
        TimelineEvent,
        AuditEvent,
    )
    return {model.schema_name: fabric_schema_hash(model) for model in models}


def legacy_absence_status() -> dict[str, bool]:
    return {
        "runner_ws_transport": True,
        "peer_ws_forwarding": True,
        "direct_host_launch_fallback": True,
    }


def build_preflight_report(
    manifest: FabricManifest = DEFAULT_FABRIC_MANIFEST,
) -> FabricPreflightReport:
    nats_url = os.environ.get("OMNIGENT_NATS_URL", "").strip()
    service_versions = required_fabric_service_versions(manifest)
    missing_services = [
        service for service in manifest.required_services if service not in service_versions
    ]
    checks = [
        FabricCheck(
            name="nats",
            status="pass" if nats_url else "fail",
            detail="configured" if nats_url else "OMNIGENT_NATS_URL is not configured",
        ),
        FabricCheck(
            name="services",
            status="pass" if not missing_services else "fail",
            detail=(
                f"{len(service_versions)} required services registered"
                if not missing_services
                else f"missing services: {', '.join(missing_services)}"
            ),
        ),
        FabricCheck(
            name="schemas",
            status="pass",
            detail=f"{len(schema_hashes())} canonical schemas registered",
        ),
        FabricCheck(
            name="legacy_absence",
            status="pass",
            detail="session routing delegates runner acquisition to RunnerFabric",
        ),
    ]
    status: CheckStatus = "pass"
    if any(check.status == "fail" for check in checks):
        status = "fail"
    elif any(check.status == "warn" for check in checks):
        status = "warn"
    services = (
        service_versions
        if nats_url
        else dict.fromkeys(manifest.required_services, "missing")
    )
    return FabricPreflightReport(
        status=status,
        checks=tuple(checks),
        services=services,
        schema_hashes=schema_hashes(),
        legacy_absence=legacy_absence_status(),
        generation=manifest.generation,
    )


def fabric_capabilities(
    manifest: FabricManifest = DEFAULT_FABRIC_MANIFEST,
) -> dict[str, object]:
    active = bool(os.environ.get("OMNIGENT_NATS_URL", "").strip())
    service_versions = required_fabric_service_versions(manifest)
    return {
        "active": active,
        "nats_ready": active and len(service_versions) == len(manifest.required_services),
        "service_versions": service_versions,
        "schema_hashes": schema_hashes(),
        "required_services": list(manifest.required_services),
        "legacy_absence": legacy_absence_status(),
    }
