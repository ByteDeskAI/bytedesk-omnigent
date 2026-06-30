"""Tests for the agent definition manifest store."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import yaml

from omnigent.stores.agent_definition_store import (
    AgentDefinitionStore,
    file_key,
    head_key,
)
from omnigent.stores.artifact_store.local import LocalArtifactStore


def _bundle(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_put_bundle_writes_head_manifest_and_file_blobs(tmp_path: Path) -> None:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    store = AgentDefinitionStore(artifact_store)
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "demo",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        }
    )
    bundle = _bundle(
        {
            "config.yaml": config,
            "AGENTS.md": "Use this agent.\n",
            "skills/example/SKILL.md": "---\nname: example\ndescription: Example.\n---\n",
            "skills/example/icon.bin": b"\x00\x01",
        }
    )

    manifest = store.put_bundle(
        agent_id="ag_demo",
        bundle_location="ag_demo/hash",
        bundle_bytes=bundle,
    )

    assert store.get_head("ag_demo") == manifest
    assert store.get_manifest("ag_demo", manifest.bundle_sha256) == manifest
    by_path = {file.path: file for file in manifest.files}
    assert sorted(by_path) == [
        "AGENTS.md",
        "config.yaml",
        "skills/example/SKILL.md",
        "skills/example/icon.bin",
    ]
    assert by_path["skills/example/icon.bin"].binary is True
    assert artifact_store.exists(head_key("ag_demo"))
    assert artifact_store.get(file_key(by_path["config.yaml"].sha256)) == config.encode()
