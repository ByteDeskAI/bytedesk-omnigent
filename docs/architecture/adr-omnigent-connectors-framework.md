# ADR: Omnigent connectors framework

**Status:** Accepted

**Scope:** Omnigent admin, extension seams, external service credentials, agent tool grants.
Office/Platform connected-app consumption is explicitly out of scope for this phase.

## Context

Omnigent needs an admin-managed **Connectors** area for external services such as
Jira, Confluence, and Google Workspace. The implementation must stay Omnigent
centered and must not create a new route, table, or UI pattern for every provider.

The existing architecture already uses extension-contributed manifests and
Protocol/Strategy seams. Connectors should follow that same shape: the host owns
generic lifecycle and governance; extensions supply provider-specific adapters.

## Decision

Use a shared connector framework with two extension contributions:

- `connector_manifests()` contributes provider/service/auth metadata.
- `connector_providers()` contributes provider Strategy/Adapter factories.

The shared framework owns:

- catalog and admin APIs;
- connection, service, OAuth-state, and action-level grant persistence;
- service enable/disable state;
- per-agent service/action grant records;
- secret-reference storage contract;
- manifest-driven web UI rendering;
- health-check result persistence.
- server-side MCP tool interception for connector-managed tool prefixes.

Each provider adapter owns only provider-specific behavior:

- OAuth or direct credential registration;
- credential payload normalization;
- provider health validation;
- provider-specific agent tool materialization.

This creates a stable add-a-connector recipe:

1. Add a `ConnectorManifest` with services, actions, scopes, tool mounts, and setup fields.
2. Add a `ConnectorProvider` implementation.
3. Contribute both through an Omnigent extension.
4. Add provider-specific MCP execution behind the granted action names.

## Consequences

The admin router stays provider-blind: `/v1/connectors/{provider}/...` dispatches
through the provider registry. The web UI renders setup fields from the manifest
instead of hard-coding provider forms. Agent grants stay generic at
`service:action` granularity and delegate only the final bundle mutation to the
provider adapter.

Atlassian proves OAuth plus connector-managed MCP mounting for Jira and
Confluence actions. Google Workspace proves domain-wide-delegated MCP tools
over a shared keyless Workload Identity Federation credential seam. The
Workspace manifest exposes a broad service catalog and generates structured
operation tools for every supported service/operation pair. A generic HTTP
executor can exist underneath, but it is private implementation detail: agents
only receive named tools such as `drive_search`, `forms_read`,
`vault_admin_mutate`, and `vertex_ai_generate`.

Connector MCP servers are schema advertisement fronts. Runtime `google__*` and
`atlassian__*` calls are intercepted by the ByteDesk extension at the Omnigent
`tools/call` boundary, resolved against the verified session agent id, checked
against connector grants, and executed with credentials from the connector store.
This avoids putting database URLs, provider credentials, OAuth material, scopes,
or connector metadata into runner child-process environments or Kubernetes
Secret/ConfigMap objects.

The first live Google proof is Drive search. It requires the Omnigent-owned
service account `omnigent-workspace-agents@bytedesk-497319.iam.gserviceaccount.com`,
the cluster Workload Identity Federation provider, and Google Workspace Admin
domain-wide delegation for the manifest scopes. Those provider-specific values
belong in the Google Workspace connector record, not in Kubernetes deployment
manifests. Kubernetes can expose generic runtime capability, such as allowing
first-party Omnigent runtime ServiceAccounts to request a bounded token for the
host ServiceAccount when a connector record chooses Kubernetes TokenRequest as a
subject-token source, but it must not store Jira, Confluence, Google Workspace,
or other connector identities, audiences, subjects, scopes, or service grants.

Future providers should not add connector-specific admin routes or deployment
configuration unless a new generic lifecycle capability is first identified and
added to the provider contract.
