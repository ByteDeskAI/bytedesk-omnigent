import { useEffect, useMemo, useState } from "react";
import { PlugIcon, RefreshCwIcon, ShieldCheckIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { getMe } from "@/lib/accountsApi";
import { useNavigate } from "@/lib/routing";
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
import type { ConnectorConnection, ConnectorManifest } from "@/lib/connectorsApi";

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "connected" || status === "ready" || status === "healthy"
      ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/10"
      : status === "disabled"
        ? "text-muted-foreground border-border bg-muted/40"
        : "text-amber-300 border-amber-500/40 bg-amber-500/10";
  return <span className={`rounded-full border px-2 py-0.5 text-[11px] ${tone}`}>{status}</span>;
}

function CredentialSetup({ provider }: { provider: ConnectorManifest }) {
  const createConnector = useCreateConnectorConnection();
  const [displayName, setDisplayName] = useState(provider.name);
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

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
        <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
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
      <div className="mt-2 flex justify-end">
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

function ConnectionPanel({
  connection,
  provider,
}: {
  connection: ConnectorConnection;
  provider: ConnectorManifest;
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
  const availableTools = useMemo(
    () =>
      connection.services.flatMap((svc) => {
        if (!svc.enabled) return [];
        return (serviceDefs.get(svc.serviceKey)?.tools ?? []).map((tool) => ({
          ...tool,
          serviceKey: svc.serviceKey,
          token: `${svc.serviceKey}:${tool.key}`,
        }));
      }),
    [connection.services, serviceDefs],
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

  return (
    <div className="rounded-md border border-border bg-background p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex items-center gap-2">
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

      <div className="mt-3 grid gap-2 md:grid-cols-2">
        {connection.services.map((svc) => (
          <div
            key={svc.serviceKey}
            className="flex items-center justify-between gap-3 rounded-md border border-border/70 px-3 py-2"
          >
            <div>
              <div className="text-sm font-medium">{svc.serviceKey}</div>
              <div className="text-[11px] text-muted-foreground">
                {svc.status} · {serviceDefs.get(svc.serviceKey)?.tools.length ?? 0} actions
              </div>
            </div>
            <Switch
              size="sm"
              checked={svc.enabled}
              onCheckedChange={(enabled) =>
                toggle.mutate({ connectionId: connection.id, serviceKey: svc.serviceKey, enabled })
              }
              aria-label={`Toggle ${svc.serviceKey}`}
            />
          </div>
        ))}
      </div>

      {availableTools.length > 0 && (
        <div className="mt-3 rounded-md border border-border/70 p-3">
          <div className="mb-2 text-xs font-medium text-muted-foreground">Agent actions</div>
          <div className="grid gap-2 md:grid-cols-2">
            {availableTools.map((tool) => (
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
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
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

function ProviderPanel({ provider }: { provider: ConnectorManifest }) {
  const startOAuth = useStartConnectorOAuth();

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold">{provider.name}</h2>
            <StatusPill status={provider.auth.type} />
          </div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {provider.services.map((svc) => (
              <span
                key={svc.key}
                className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
              >
                {svc.name}
              </span>
            ))}
          </div>
        </div>
        {provider.auth.type === "oauth_3lo" && (
          <Button
            size="sm"
            onClick={() => startOAuth.mutate(provider.provider)}
            disabled={startOAuth.isPending}
          >
            Connect
          </Button>
        )}
      </div>

      {startOAuth.isError && (
        <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {startOAuth.error instanceof Error ? startOAuth.error.message : "OAuth start failed"}
        </div>
      )}

      {provider.auth.type !== "oauth_3lo" &&
        provider.auth.setupFields.length > 0 &&
        provider.connections.length === 0 && (
          <div className="mt-3">
            <CredentialSetup provider={provider} />
          </div>
        )}

      <div className="mt-3 flex flex-col gap-3">
        {provider.connections.map((connection) => (
          <ConnectionPanel key={connection.id} connection={connection} provider={provider} />
        ))}
      </div>
    </section>
  );
}

export function ConnectorsPage() {
  const navigate = useNavigate();
  const info = useServerInfo();
  const [allowed, setAllowed] = useState<boolean | null>(null);
  const { data = [], isLoading, isError, error, refetch } = useConnectorsCatalog();

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

  if (allowed === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!allowed) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Connectors</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to manage connectors.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-5xl px-6 py-8 pt-14">
      <div className="mb-6 flex items-center justify-between gap-3">
        <div className="flex items-start gap-2.5">
          <PlugIcon className="mt-1 size-5 shrink-0 text-muted-foreground" />
          <div>
            <h1 className="text-2xl font-semibold">Connectors</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              External service access for Omnigent agents.
            </p>
          </div>
        </div>
        <Button variant="ghost" size="icon" onClick={() => void refetch()}>
          <RefreshCwIcon />
        </Button>
      </div>

      {isError && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error instanceof Error ? error.message : "Failed to load connectors."}
        </div>
      )}
      {isLoading && <div className="text-sm text-muted-foreground">Loading connectors...</div>}
      {!isLoading && data.length === 0 && (
        <div className="text-sm text-muted-foreground">No connector manifests are registered.</div>
      )}
      <div className="flex flex-col gap-4">
        {data.map((provider) => (
          <ProviderPanel key={provider.provider} provider={provider} />
        ))}
      </div>
    </div>
  );
}
