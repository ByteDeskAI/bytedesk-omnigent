"""Agent definition manifests stored over the configured artifact backend."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from omnigent.spec.tar_utils import extract_safe
from omnigent.stores.artifact_store import ArtifactStore

_ROOT = "agent-definitions"
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AgentDefinitionFile:
    """One logical file in an agent definition manifest."""

    path: str
    sha256: str
    size: int
    binary: bool


@dataclass(frozen=True)
class AgentDefinitionManifest:
    """Content-addressed view of an agent image."""

    schema_version: int
    agent_id: str
    bundle_location: str
    bundle_sha256: str
    files: tuple[AgentDefinitionFile, ...]


class AgentDefinitionStore:
    """Store agent image definitions as immutable file manifests.

    The store is intentionally an adapter over :class:`ArtifactStore`. In
    production the artifact store is already backed by NATS Object Store, so
    definitions and file blobs land in NATS without changing external APIs.
    Local tests keep using the local artifact adapter.
    """

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._artifact_store = artifact_store

    def put_bundle(
        self,
        *,
        agent_id: str,
        bundle_location: str,
        bundle_bytes: bytes,
    ) -> AgentDefinitionManifest:
        """Persist a manifest and file blobs for *bundle_bytes*."""
        bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "agent"
            extract_safe(bundle_bytes, root)
            files = tuple(self._store_files(root))

        manifest = AgentDefinitionManifest(
            schema_version=_SCHEMA_VERSION,
            agent_id=agent_id,
            bundle_location=bundle_location,
            bundle_sha256=bundle_sha,
            files=files,
        )
        manifest_bytes = _manifest_to_json(manifest)
        self._artifact_store.put(manifest_key(agent_id, bundle_sha), manifest_bytes)
        self._artifact_store.put(head_key(agent_id), manifest_bytes)
        return manifest

    def get_head(self, agent_id: str) -> AgentDefinitionManifest:
        """Return the current manifest for *agent_id*."""
        return _manifest_from_json(self._artifact_store.get(head_key(agent_id)))

    def get_manifest(self, agent_id: str, bundle_sha256: str) -> AgentDefinitionManifest:
        """Return a specific immutable manifest revision."""
        return _manifest_from_json(self._artifact_store.get(manifest_key(agent_id, bundle_sha256)))

    def _store_files(self, root: Path) -> list[AgentDefinitionFile]:
        files: list[AgentDefinitionFile] = []
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            rel = path.relative_to(root).as_posix()
            self._artifact_store.put(file_key(digest), data)
            files.append(
                AgentDefinitionFile(
                    path=rel,
                    sha256=digest,
                    size=len(data),
                    binary=_is_binary(data),
                )
            )
        return files


def head_key(agent_id: str) -> str:
    """Artifact key for an agent's current definition head."""
    return f"{_ROOT}/heads/{agent_id}.json"


def manifest_key(agent_id: str, bundle_sha256: str) -> str:
    """Artifact key for an immutable definition manifest."""
    return f"{_ROOT}/manifests/{agent_id}/{bundle_sha256}.json"


def file_key(sha256: str) -> str:
    """Artifact key for an immutable file blob."""
    return f"{_ROOT}/files/{sha256}"


def _manifest_to_json(manifest: AgentDefinitionManifest) -> bytes:
    payload = asdict(manifest)
    payload["files"] = [asdict(file) for file in manifest.files]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _manifest_from_json(data: bytes) -> AgentDefinitionManifest:
    payload = json.loads(data.decode())
    return AgentDefinitionManifest(
        schema_version=int(payload["schema_version"]),
        agent_id=str(payload["agent_id"]),
        bundle_location=str(payload["bundle_location"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        files=tuple(AgentDefinitionFile(**file) for file in payload.get("files", [])),
    )


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data
