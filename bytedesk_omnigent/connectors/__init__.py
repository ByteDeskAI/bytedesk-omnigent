"""Omnigent-owned external service connectors.

Connectors are durable, admin-managed external tool providers. They are separate
from goal-engine connected apps: a connector stores auth/service state and grants
agent tool access; connected apps register sensors/actuators for goals.
"""

from bytedesk_omnigent.connectors.manifests import (
    ConnectorAuthSpec,
    ConnectorManifest,
    ConnectorService,
    ConnectorSetupField,
    ConnectorTool,
    atlassian_connector_manifest,
    bytedesk_connector_manifests,
    google_workspace_connector_manifest,
)
from bytedesk_omnigent.connectors.providers import (
    AtlassianConnectorProvider,
    ConnectorCreateRequest,
    ConnectorHealthResult,
    ConnectorOAuthCallbackRequest,
    ConnectorOAuthStartRequest,
    ConnectorOAuthStartResult,
    ConnectorProvider,
    GoogleWorkspaceConnectorProvider,
    bytedesk_connector_providers,
)
from bytedesk_omnigent.connectors.registry import ConnectorRegistry, build_connector_registry
from bytedesk_omnigent.connectors.store import (
    ConnectorAgentGrant,
    ConnectorConnection,
    ConnectorOAuthState,
    ConnectorServiceState,
    SqlAlchemyConnectorStore,
    get_connector_store,
)

__all__ = [
    "AtlassianConnectorProvider",
    "ConnectorAgentGrant",
    "ConnectorAuthSpec",
    "ConnectorConnection",
    "ConnectorCreateRequest",
    "ConnectorHealthResult",
    "ConnectorManifest",
    "ConnectorOAuthCallbackRequest",
    "ConnectorOAuthStartRequest",
    "ConnectorOAuthStartResult",
    "ConnectorOAuthState",
    "ConnectorProvider",
    "ConnectorRegistry",
    "ConnectorService",
    "ConnectorServiceState",
    "ConnectorSetupField",
    "ConnectorTool",
    "GoogleWorkspaceConnectorProvider",
    "SqlAlchemyConnectorStore",
    "atlassian_connector_manifest",
    "build_connector_registry",
    "bytedesk_connector_manifests",
    "bytedesk_connector_providers",
    "get_connector_store",
    "google_workspace_connector_manifest",
]
