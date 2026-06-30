"""Connector grant materialization for template agents."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from bytedesk_omnigent.connectors.registry import build_connector_registry
from bytedesk_omnigent.connectors.store import (
    ConnectorAgentGrant,
    ConnectorConnection,
    ConnectorServiceState,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.agent_write import apply_bundle_update
from omnigent.server.bundles import validate_agent_bundle
from omnigent.spec.tar_utils import build_bundle_bytes


def _load_config(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text()) if path.is_file() else {}
    return loaded if isinstance(loaded, dict) else {}


def materialize_agent_connector_grant(
    *,
    connection: ConnectorConnection,
    services: list[ConnectorServiceState],
    grants: list[ConnectorAgentGrant],
    agent_id: str,
    agent_store: Any | None = None,
    agent_cache: Any | None = None,
    artifact_store: Any | None = None,
) -> None:
    """Apply connector-managed tool entries to a template agent image."""
    if agent_store is None or agent_cache is None or artifact_store is None:
        from omnigent.runtime import get_agent_cache, get_agent_store, get_artifact_store

        agent_store = agent_store or get_agent_store()
        agent_cache = agent_cache or get_agent_cache()
        artifact_store = artifact_store or get_artifact_store()
    agent = agent_store.get(agent_id)
    if agent is None:
        raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND)
    if agent.session_id is not None:
        raise OmnigentError(
            "connector grants can only materialize onto template agents",
            code=ErrorCode.INVALID_INPUT,
        )

    loaded = agent_cache.load(agent.id, agent.bundle_location, expand_env=False)
    staging = Path(tempfile.mkdtemp(prefix=f"{agent.id}_connector_"))
    try:
        shutil.copytree(loaded.workdir, staging, dirs_exist_ok=True)
        config_path = staging / "config.yaml"
        config = _load_config(config_path)
        provider = build_connector_registry().get_provider(connection.provider)
        if provider is None:
            raise OmnigentError(
                f"unknown connector provider: {connection.provider}",
                code=ErrorCode.INVALID_INPUT,
            )
        provider.apply_agent_grant(
            staging=staging,
            config=config,
            connection=connection,
            services=services,
            grants=grants,
        )
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        bundle_bytes = build_bundle_bytes(staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    spec = validate_agent_bundle(bundle_bytes, enforce_handler_allowlist=False)
    if spec.name != agent.name:
        raise OmnigentError(
            f"spec name '{spec.name}' does not match agent '{agent.name}'",
            code=ErrorCode.INVALID_INPUT,
        )
    apply_bundle_update(
        agent,
        bundle_bytes,
        artifact_store=artifact_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        expand_env=True,
    )
