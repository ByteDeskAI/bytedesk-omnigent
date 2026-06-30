import { type ReactNode, useEffect, useMemo, useState } from "react";
import {
  ArrowLeftIcon,
  ExternalLinkIcon,
  PlugIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { getMe } from "@/lib/accountsApi";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  useCheckConnectorHealth,
  useConnectorsCatalog,
  useCreateConnectorConnection,
  useGrantConnectorToAgent,
  useSetConnectorServiceEnabled,
  useStartConnectorOAuth,
} from "@/hooks/useConnectors";
import type {
  ConnectorConnection,
  ConnectorManifest,
  ConnectorTool,
} from "@/lib/connectorsApi";
import {
  buildConnectorPresentation,
  type ConnectorPresentation,
} from "@/lib/connectorPresentation";

interface AvailableConnectorTool extends ConnectorTool {
  serviceKey: string;
  serviceName: string;
  token: string;
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "connected" || status === "ready" || status === "healthy"
      ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/10"
      : status === "disabled" || status === "not connected"
        ? "text-muted-foreground border-border bg-muted/40"
        : "text-amber-300 border-amber-500/40 bg-amber-500/10";
  return <span className={`rounded-full border px-2 py-0.5 text-[11px] ${tone}`}>{status}</span>;
}

function ConnectorPageShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 overflow-y-auto" data-testid="connector-page-scroll">
      <div className="mx-auto w-full max-w-6xl px-6 pt-14 pb-16">{children}</div>
    </div>
  );
}

function ConnectorBreadcrumbs({ providerName }: { providerName?: string }) {
  return (
    <nav aria-label="Breadcrumb" className="mb-4 text-xs text-muted-foreground">
      <ol className="flex flex-wrap items-center gap-1.5">
        <li>
          {providerName ? (
            <Link to="/connectors" className="hover:text-foreground hover:underline">
              Connectors
            </Link>
          ) : (
            <span className="text-foreground">Connectors</span>
          )}
        </li>
        {providerName && (
          <>
            <li aria-hidden="true">/</li>
            <li className="text-foreground">{providerName}</li>
          </>
        )}
      </ol>
    </nav>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="rounded-md border border-border/70 bg-muted/20 px-2 py-1 text-xs text-muted-foreground">
      <span className="font-medium text-foreground">{value}</span> {label}
    </span>
  );
}

function useConnectorAdminAccess() {
  const navigate = useNavigate();
  const info = useServerInfo();
  const [allowed, setAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    if (info === "loading") return;
    if (!info.accounts_enabled) {
      setAllowed(true);
      return;
    }
    void (async () => {
      const me = await getMe();
      if (me === null) {
        navigate("/login", { replace: true });
        return;
      }
      setAllowed(me.is_admin);
    })();
  }, [info, navigate]);

  return allowed;
}

function ConnectorAccessGate({
  allowed,
  children,
}: {
  allowed: boolean | null;
  children: ReactNode;
}) {
  if (allowed === null) {
    return (
      <ConnectorPageShell>
        <div className="flex min-h-80 items-center justify-center text-sm text-muted-foreground">
          Loading...
        </div>
      </ConnectorPageShell>
    );
  }

  if (!allowed) {
    return (
      <ConnectorPageShell>
        <div className="max-w-2xl">
          <h1 className="mb-2 text-2xl font-semibold">Connectors</h1>
          <p className="text-sm text-muted-foreground">
            You don't have permission to manage connectors.
          </p>
        </div>
      </ConnectorPageShell>
    );
  }

  return children;
}

function CredentialSetup({ provider }: { provider: ConnectorManifest }) {
  const createConnector = useCreateConnectorConnection();
  const [displayName, setDisplayName] = useState(provider.name);
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDisplayName(provider.name);
    setValues({});
    setError(null);
  }, [provider.provider, provider.name]);

  async function submit() {
    setError(null);
    const metadata: Record<string, unknown> = {};
    const secretPayload: Record<string, unknown> = {};
    for (const field of provider.auth.setupFields) {
      const raw = values[field.key]?.trim() ?? "";
      if (field.required && !raw) {
        setError(`${field.label} is required.`);
        return;
      }
      if (!raw) continue;
      let value: unknown = raw;
      if (field.input === "json_secret") {
        try {
          value = JSON.parse(raw) as Record<string, unknown>;
        } catch {
          setError(`${field.label} is invalid JSON.`);
          return;
        }
      }
      if (field.target === "secret_payload") {
        secretPayload[field.key] = value;
      } else {
        metadata[field.key] = value;
      }
    }
    await createConnector.mutateAsync({
      provider: provider.provider,
      displayName,
      metadata,
      secretPayload: Object.keys(secretPayload).length > 0 ? secretPayload : undefined,
      enabledServices: provider.services.map((svc) => svc.key),
    });
    setValues({});
  }

  return (
    <div className="rounded-md border border-border bg-muted/20 p-3">
      <div className="grid gap-2 md:grid-cols-2">
        <Input
          aria-label="Connection name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
        />
        {provider.auth.setupFields
          .filter((field) => field.input === "text")
          .map((field) => (
            <Input
              key={field.key}
              placeholder={field.label}
              value={values[field.key] ?? ""}
              onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
            />
          ))}
      </div>
      {provider.auth.setupFields
        .filter((field) => field.input === "json_secret")
        .map((field) => (
          <Textarea
            key={field.key}
            className="mt-2 min-h-28 font-mono text-xs"
            placeholder={field.label}
            value={values[field.key] ?? ""}
            onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
          />
        ))}
      {error && <div className="mt-2 text-xs text-destructive">{error}</div>}
      {createConnector.isError && (
        <div className="mt-2 text-xs text-destructive">
          {createConnector.error instanceof Error
            ? createConnector.error.message
            : "Connection failed"}
        </div>
      )}
      <div className="mt-3 flex justify-end">
        <Button
          size="sm"
          onClick={() => void submit()}
          disabled={!displayName || createConnector.isPending}
        >
          Connect
        </Button>
      </div>
    </div>
  );
}

function groupConnectionServices(
  connection: ConnectorConnection,
  presentation: ConnectorPresentation,
) {
  const statesByKey = new Map(connection.services.map((service) => [service.serviceKey, service]));
  return presentation.serviceGroups
    .map((group) => ({
      ...group,
      services: group.services.flatMap((service) => {
        const state = statesByKey.get(service.key);
        if (!state) return [];
        return [{ definition: service, state }];
      }),
    }))
    .filter((group) => group.services.length > 0);
}

function ConnectionPanel({
  connection,
  provider,
  presentation,
}: {
  connection: ConnectorConnection;
  provider: ConnectorManifest;
  presentation: ConnectorPresentation;
}) {
  const toggle = useSetConnectorServiceEnabled();
  const health = useCheckConnectorHealth();
  const grant = useGrantConnectorToAgent();
  const { data: agents = [] } = useAvailableAgents();
  const [agentId, setAgentId] = useState("");
  const serviceDefs = useMemo(
    () => new Map(provider.services.map((service) => [service.key, service])),
    [provider.services],
  );
  const availableTools = useMemo<AvailableConnectorTool[]>(
    () =>
      connection.services.flatMap((svc) => {
        if (!svc.enabled) return [];
        const service = serviceDefs.get(svc.serviceKey);
        if (!service) return [];
        return service.tools.map((tool) => ({
          ...tool,
          serviceKey: svc.serviceKey,
          serviceName: service.name,
          token: `${svc.serviceKey}:${tool.key}`,
        }));
      }),
    [connection.services, serviceDefs],
  );
  const serviceGroups = useMemo(
    () => groupConnectionServices(connection, presentation),
    [connection, presentation],
  );
  const actionGroups = useMemo(
    () =>
      presentation.serviceGroups
        .map((group) => ({
          ...group,
          services: group.services
            .map((service) => ({
              definition: service,
              tools: availableTools.filter((tool) => tool.serviceKey === service.key),
            }))
            .filter((service) => service.tools.length > 0),
        }))
        .filter((group) => group.services.length > 0),
    [availableTools, presentation],
  );
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const healthResult = health.data?.connection?.id === connection.id ? health.data : null;
  const healthMetadata = healthResult?.metadata ?? {};
  const requiredScopes = Array.isArray(healthMetadata.requiredScopes)
    ? healthMetadata.requiredScopes.filter((scope): scope is string => typeof scope === "string")
    : [];
  const clientId =
    typeof healthMetadata.clientId === "string" ? healthMetadata.clientId : undefined;

  useEffect(() => {
    setSelectedTools(availableTools.map((tool) => tool.token));
  }, [availableTools]);

  function setToolSelected(token: string, checked: boolean) {
    setSelectedTools((prev) => {
      if (checked && !prev.includes(token)) return [...prev, token];
      if (!checked) return prev.filter((item) => item !== token);
      return prev;
    });
  }

  function setToolsSelected(tokens: string[], checked: boolean) {
    setSelectedTools((prev) => {
      const next = new Set(prev);
      for (const token of tokens) {
        if (checked) {
          next.add(token);
        } else {
          next.delete(token);
        }
      }
      return Array.from(next);
    });
  }

  return (
    <div className="rounded-md border border-border bg-background p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-medium">{connection.displayName}</h3>
            <StatusPill status={connection.status} />
            {connection.lastHealthStatus && <StatusPill status={connection.lastHealthStatus} />}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {connection.authType} · {connection.services.length} services ·{" "}
            {connection.grants.length} tool grants
          </div>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={() =>
            health.mutate({
              connectionId: connection.id,
              live: connection.provider === "google_workspace",
            })
          }
          disabled={health.isPending}
        >
          <RefreshCwIcon /> Test
        </Button>
      </div>

      {(connection.lastError || healthResult?.ok === false) && (
        <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
          <div className="font-medium">
            {connection.lastError ?? "Connector health check failed"}
          </div>
          {clientId && <div className="mt-1 text-amber-100/80">Client ID: {clientId}</div>}
          {requiredScopes.length > 0 && (
            <div className="mt-1 text-amber-100/80">
              Required scopes: {requiredScopes.join(", ")}
            </div>
          )}
        </div>
      )}

      {serviceGroups.length > 0 && (
        <section className="mt-4">
          <h4 className="mb-2 text-xs font-medium text-muted-foreground">Services</h4>
          <div className="space-y-3">
            {serviceGroups.map((group) => (
              <div key={group.key} className="rounded-md border border-border/70 p-3">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-medium">{group.name}</div>
                  <div className="text-[11px] text-muted-foreground">
                    {group.services.length} services
                  </div>
                </div>
                <div className="grid gap-2 md:grid-cols-2">
                  {group.services.map(({ definition, state }) => (
                    <div
                      key={state.serviceKey}
                      className="flex items-center justify-between gap-3 rounded-md border border-border/60 px-3 py-2"
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">{definition.name}</div>
                        <div className="text-[11px] text-muted-foreground">
                          {state.status} · {definition.tools.length} actions
                        </div>
                      </div>
                      <Switch
                        size="sm"
                        checked={state.enabled}
                        onCheckedChange={(enabled) =>
                          toggle.mutate({
                            connectionId: connection.id,
                            serviceKey: state.serviceKey,
                            enabled,
                          })
                        }
                        aria-label={`Toggle ${definition.name}`}
                      />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {availableTools.length > 0 && (
        <section className="mt-4 rounded-md border border-border/70 p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h4 className="text-xs font-medium text-muted-foreground">Agent actions</h4>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Select the actions this connection should materialize for the agent.
              </p>
            </div>
            <div className="text-[11px] text-muted-foreground">
              {selectedTools.length}/{availableTools.length} selected
            </div>
          </div>
          <div className="max-h-[36rem] space-y-3 overflow-y-auto pr-1">
            {actionGroups.map((group) => (
              <div key={group.key} className="rounded-md border border-border/60 p-3">
                <div className="mb-2 text-sm font-medium">{group.name}</div>
                <div className="space-y-3">
                  {group.services.map(({ definition, tools }) => {
                    const tokens = tools.map((tool) => tool.token);
                    return (
                      <div key={definition.key} className="rounded-md bg-muted/20 p-2">
                        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <div className="text-sm font-medium">{definition.name}</div>
                            <div className="text-[11px] text-muted-foreground">
                              {tools.length} actions
                            </div>
                          </div>
                          <div className="flex gap-1">
                            <Button
                              type="button"
                              size="xs"
                              variant="ghost"
                              onClick={() => setToolsSelected(tokens, true)}
                            >
                              Select all
                            </Button>
                            <Button
                              type="button"
                              size="xs"
                              variant="ghost"
                              onClick={() => setToolsSelected(tokens, false)}
                            >
                              Clear
                            </Button>
                          </div>
                        </div>
                        <div className="grid gap-2 md:grid-cols-2">
                          {tools.map((tool) => (
                            <label key={tool.token} className="flex items-start gap-2 text-sm">
                              <input
                                type="checkbox"
                                className="mt-1 size-3.5"
                                checked={selectedTools.includes(tool.token)}
                                onChange={(e) => setToolSelected(tool.token, e.target.checked)}
                              />
                              <span>
                                <span className="font-medium">{tool.name}</span>
                                <span className="ml-1 text-[11px] text-muted-foreground">
                                  {tool.serviceKey}:{tool.key}
                                </span>
                              </span>
                            </label>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <select
          className="h-8 min-w-56 rounded-md border border-input bg-background px-2 text-sm"
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
          aria-label="Agent"
        >
          <option value="">Select agent</option>
          {agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.display_name}
            </option>
          ))}
        </select>
        <Button
          size="sm"
          onClick={() =>
            grant.mutate({ connectionId: connection.id, agentId, tools: selectedTools })
          }
          disabled={!agentId || selectedTools.length === 0 || grant.isPending}
        >
          <ShieldCheckIcon /> Grant
        </Button>
        {grant.isError && (
          <span className="text-xs text-destructive">
            {grant.error instanceof Error ? grant.error.message : "Grant failed"}
          </span>
        )}
      </div>
    </div>
  );
}

function ProviderCatalogCard({ provider }: { provider: ConnectorManifest }) {
  const startOAuth = useStartConnectorOAuth();
  const presentation = useMemo(() => buildConnectorPresentation(provider), [provider]);
  const connected = provider.connections.length > 0;

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold">{provider.name}</h2>
            <StatusPill status={presentation.summary.status} />
            <StatusPill status={provider.auth.type} />
          </div>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{provider.description}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Metric label="connections" value={presentation.summary.connectionCount} />
            <Metric label="services" value={presentation.summary.serviceCount} />
            <Metric label="actions" value={presentation.summary.actionCount} />
            <Metric label="grants" value={presentation.summary.grantCount} />
          </div>
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          {!connected && provider.auth.type === "oauth_3lo" && (
            <Button
              size="sm"
              onClick={() => startOAuth.mutate(provider.provider)}
              disabled={startOAuth.isPending}
            >
              Connect
            </Button>
          )}
          <Button asChild size="sm" variant="outline">
            <Link to={`/connectors/${encodeURIComponent(provider.provider)}`}>Configure</Link>
          </Button>
        </div>
      </div>

      {startOAuth.isError && (
        <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {startOAuth.error instanceof Error ? startOAuth.error.message : "OAuth start failed"}
        </div>
      )}
    </section>
  );
}

function ConnectorHeader({
  title,
  description,
  icon,
  action,
}: {
  title: string;
  description: string;
  icon: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 items-start gap-2.5">
        {icon}
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold">{title}</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{description}</p>
        </div>
      </div>
      {action}
    </div>
  );
}

export function ConnectorsPage() {
  const allowed = useConnectorAdminAccess();
  const { data = [], isLoading, isError, error, refetch } = useConnectorsCatalog();

  return (
    <ConnectorAccessGate allowed={allowed}>
      <ConnectorPageShell>
        <ConnectorBreadcrumbs />
        <ConnectorHeader
          title="Connectors"
          description="External service access for Omnigent agents."
          icon={<PlugIcon className="mt-1 size-5 shrink-0 text-muted-foreground" />}
          action={
            <Button variant="ghost" size="icon" onClick={() => void refetch()}>
              <RefreshCwIcon />
            </Button>
          }
        />

        {isError && (
          <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error instanceof Error ? error.message : "Failed to load connectors."}
          </div>
        )}
        {isLoading && <div className="text-sm text-muted-foreground">Loading connectors...</div>}
        {!isLoading && data.length === 0 && (
          <div className="text-sm text-muted-foreground">
            No connector manifests are registered.
          </div>
        )}
        <div className="flex flex-col gap-4">
          {data.map((provider) => (
            <ProviderCatalogCard key={provider.provider} provider={provider} />
          ))}
        </div>
      </ConnectorPageShell>
    </ConnectorAccessGate>
  );
}

function ProviderSelector({
  providers,
  provider,
}: {
  providers: ConnectorManifest[];
  provider: ConnectorManifest;
}) {
  const navigate = useNavigate();
  if (providers.length < 2) return null;
  return (
    <select
      className="h-8 min-w-48 rounded-md border border-input bg-background px-2 text-sm"
      value={provider.provider}
      onChange={(event) => navigate(`/connectors/${encodeURIComponent(event.target.value)}`)}
      aria-label="Connector provider"
    >
      {providers.map((item) => (
        <option key={item.provider} value={item.provider}>
          {item.name}
        </option>
      ))}
    </select>
  );
}

function ConnectionSelector({
  connections,
  selectedConnectionId,
  onChange,
}: {
  connections: ConnectorConnection[];
  selectedConnectionId: string;
  onChange: (connectionId: string) => void;
}) {
  if (connections.length < 2) return null;
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2">
      <label className="text-sm font-medium" htmlFor="connector-connection">
        Connection
      </label>
      <select
        id="connector-connection"
        className="h-8 min-w-56 rounded-md border border-input bg-background px-2 text-sm"
        value={selectedConnectionId}
        onChange={(event) => onChange(event.target.value)}
      >
        {connections.map((connection) => (
          <option key={connection.id} value={connection.id}>
            {connection.displayName}
          </option>
        ))}
      </select>
    </div>
  );
}

function ProviderDetail({
  provider,
  providers,
  refetch,
}: {
  provider: ConnectorManifest;
  providers: ConnectorManifest[];
  refetch: () => unknown;
}) {
  const startOAuth = useStartConnectorOAuth();
  const presentation = useMemo(() => buildConnectorPresentation(provider), [provider]);
  const [selectedConnectionId, setSelectedConnectionId] = useState(
    provider.connections[0]?.id ?? "",
  );

  useEffect(() => {
    if (provider.connections.length === 0) {
      setSelectedConnectionId("");
      return;
    }
    if (!provider.connections.some((connection) => connection.id === selectedConnectionId)) {
      setSelectedConnectionId(provider.connections[0]?.id ?? "");
    }
  }, [provider.connections, selectedConnectionId]);

  const selectedConnection =
    provider.connections.find((connection) => connection.id === selectedConnectionId) ??
    provider.connections[0];
  const connected = provider.connections.length > 0;

  return (
    <>
      <ConnectorBreadcrumbs providerName={provider.name} />
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link to="/connectors">
            <ArrowLeftIcon /> Back
          </Link>
        </Button>
        <ProviderSelector providers={providers} provider={provider} />
      </div>

      <ConnectorHeader
        title={provider.name}
        description={provider.description}
        icon={<PlugIcon className="mt-1 size-5 shrink-0 text-muted-foreground" />}
        action={
          <div className="flex flex-wrap justify-end gap-2">
            {provider.auth.docsUrl && (
              <Button asChild variant="ghost" size="sm">
                <a href={provider.auth.docsUrl} target="_blank" rel="noreferrer">
                  Docs <ExternalLinkIcon />
                </a>
              </Button>
            )}
            <Button variant="ghost" size="icon" onClick={() => void refetch()}>
              <RefreshCwIcon />
            </Button>
          </div>
        }
      />

      <div className="mb-4 flex flex-wrap gap-2">
        <StatusPill status={presentation.summary.status} />
        <StatusPill status={provider.auth.type} />
        <Metric label="connections" value={presentation.summary.connectionCount} />
        <Metric label="services" value={presentation.summary.serviceCount} />
        <Metric label="actions" value={presentation.summary.actionCount} />
        <Metric label="enabled services" value={presentation.summary.enabledServiceCount} />
        <Metric label="grants" value={presentation.summary.grantCount} />
      </div>

      {startOAuth.isError && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {startOAuth.error instanceof Error ? startOAuth.error.message : "OAuth start failed"}
        </div>
      )}

      {!connected && provider.auth.type === "oauth_3lo" && (
        <div className="mb-4 rounded-md border border-border bg-card p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-medium">Connect {provider.name}</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Start the provider authorization flow before configuring services and grants.
              </p>
            </div>
            <Button onClick={() => startOAuth.mutate(provider.provider)} disabled={startOAuth.isPending}>
              Connect
            </Button>
          </div>
        </div>
      )}

      {!connected && provider.auth.type !== "oauth_3lo" && provider.auth.setupFields.length > 0 && (
        <div className="mb-4 rounded-md border border-border bg-card p-4">
          <h2 className="mb-3 font-medium">Connection setup</h2>
          <CredentialSetup provider={provider} />
        </div>
      )}

      {connected && (
        <div className="flex flex-col gap-4">
          <ConnectionSelector
            connections={provider.connections}
            selectedConnectionId={selectedConnection?.id ?? ""}
            onChange={setSelectedConnectionId}
          />
          {selectedConnection && (
            <ConnectionPanel
              key={selectedConnection.id}
              connection={selectedConnection}
              provider={provider}
              presentation={presentation}
            />
          )}
        </div>
      )}
    </>
  );
}

export function ConnectorDetailPage() {
  const allowed = useConnectorAdminAccess();
  const { provider: providerParam } = useParams<{ provider: string }>();
  const { data = [], isLoading, isError, error, refetch } = useConnectorsCatalog();
  const providerKey = providerParam ?? "";
  const provider = data.find((item) => item.provider === providerKey);

  return (
    <ConnectorAccessGate allowed={allowed}>
      <ConnectorPageShell>
        {isError && (
          <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error instanceof Error ? error.message : "Failed to load connectors."}
          </div>
        )}
        {isLoading && <div className="text-sm text-muted-foreground">Loading connectors...</div>}
        {!isLoading && !provider && (
          <>
            <ConnectorBreadcrumbs providerName="Not found" />
            <div className="max-w-2xl">
              <h1 className="mb-2 text-2xl font-semibold">Connector not found</h1>
              <p className="mb-4 text-sm text-muted-foreground">
                No connector manifest is registered for {providerKey || "this provider"}.
              </p>
              <Button asChild variant="outline">
                <Link to="/connectors">Back to connectors</Link>
              </Button>
            </div>
          </>
        )}
        {provider && <ProviderDetail provider={provider} providers={data} refetch={refetch} />}
      </ConnectorPageShell>
    </ConnectorAccessGate>
  );
}
