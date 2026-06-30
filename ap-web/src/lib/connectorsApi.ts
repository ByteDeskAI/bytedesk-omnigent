import { authenticatedFetch } from "@/lib/identity";

export interface ConnectorService {
  key: string;
  name: string;
  description: string;
  scopes: string[];
  toolMounts: string[];
  tools: ConnectorTool[];
}

export interface ConnectorTool {
  key: string;
  name: string;
  description: string;
  mcpTool: string;
  scopes: string[];
}

export interface ConnectorSetupField {
  key: string;
  label: string;
  target: "metadata" | "secret_payload";
  input: "text" | "json_secret";
  required: boolean;
  description?: string | null;
}

export interface ConnectorManifest {
  provider: string;
  name: string;
  description: string;
  auth: {
    type: string;
    scopes: string[];
    docsUrl?: string | null;
    setupFields: ConnectorSetupField[];
  };
  services: ConnectorService[];
  connections: ConnectorConnection[];
}

export interface ConnectorServiceState {
  id: string;
  connectionId: string;
  serviceKey: string;
  enabled: boolean;
  status: string;
  scopes: string[];
  metadata: Record<string, unknown>;
  updatedAt: number;
  version: number;
}

export interface ConnectorAgentGrant {
  id: string;
  connectionId: string;
  agentId: string;
  serviceKey: string;
  toolKey: string;
  enabled: boolean;
  status: string;
  metadata: Record<string, unknown>;
  createdAt: number;
  updatedAt: number;
  version: number;
}

export interface ConnectorConnection {
  id: string;
  provider: string;
  displayName: string;
  authType: string;
  status: string;
  scopes: string[];
  metadata: Record<string, unknown>;
  secretPresent: boolean;
  lastHealthStatus: string | null;
  lastHealthAt: number | null;
  lastError: string | null;
  createdAt: number;
  updatedAt: number;
  version: number;
  services: ConnectorServiceState[];
  grants: ConnectorAgentGrant[];
}

export interface ConnectorHealthCheckResult {
  ok: boolean;
  connection: ConnectorConnection | null;
  metadata: Record<string, unknown>;
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export async function fetchConnectorCatalog(): Promise<ConnectorManifest[]> {
  const res = await authenticatedFetch("/v1/connectors/catalog");
  const body = await jsonOrThrow<{ providers: ConnectorManifest[] }>(res);
  return body.providers;
}

export async function startConnectorOAuth(provider: string): Promise<string> {
  const res = await authenticatedFetch(
    `/v1/connectors/${encodeURIComponent(provider)}/oauth/start`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({}),
    },
  );
  const body = await jsonOrThrow<{ authorizationUrl: string }>(res);
  return body.authorizationUrl;
}

export async function createConnectorConnection(
  provider: string,
  body: {
    displayName: string;
    metadata: Record<string, unknown>;
    secretPayload?: Record<string, unknown>;
    secretRef?: string;
    enabledServices: string[];
  },
): Promise<ConnectorConnection> {
  const res = await authenticatedFetch(
    `/v1/connectors/${encodeURIComponent(provider)}/connections`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  const payload = await jsonOrThrow<{ connection: ConnectorConnection }>(res);
  return payload.connection;
}

export async function setConnectorServiceEnabled(
  connectionId: string,
  serviceKey: string,
  enabled: boolean,
): Promise<ConnectorServiceState> {
  const res = await authenticatedFetch(
    `/v1/connectors/connections/${encodeURIComponent(connectionId)}/services/${encodeURIComponent(
      serviceKey,
    )}`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ enabled }),
    },
  );
  const body = await jsonOrThrow<{ service: ConnectorServiceState }>(res);
  return body.service;
}

export async function checkConnectorHealth(
  connectionId: string,
  options: { live?: boolean } = {},
): Promise<ConnectorHealthCheckResult> {
  const suffix = options.live ? "?live=true" : "";
  const res = await authenticatedFetch(
    `/v1/connectors/connections/${encodeURIComponent(connectionId)}/health-check${suffix}`,
    { method: "POST" },
  );
  return jsonOrThrow<ConnectorHealthCheckResult>(res);
}

export async function grantConnectorToAgent(
  connectionId: string,
  agentId: string,
  tools: string[],
): Promise<ConnectorAgentGrant[]> {
  const res = await authenticatedFetch(
    `/v1/connectors/connections/${encodeURIComponent(connectionId)}/agent-grants`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ agentId, tools, enabled: true, replace: true, materialize: true }),
    },
  );
  const body = await jsonOrThrow<{ grants: ConnectorAgentGrant[] }>(res);
  return body.grants;
}
