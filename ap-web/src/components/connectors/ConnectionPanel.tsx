import { useEffect, useMemo, useState } from "react";
import { RefreshCwIcon, ShieldCheckIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  useCheckConnectorHealth,
  useGrantConnectorToAgent,
  useSetConnectorServiceEnabled,
} from "@/hooks/useConnectors";
import type { ConnectorConnection, ConnectorManifest } from "@/lib/connectorsApi";
import type { ConnectorPresentation } from "@/lib/connectorPresentation";
import {
  groupConnectionServices,
  type AvailableConnectorTool,
} from "./connectors-utils";
import { StatusPill } from "./StatusPill";

export function ConnectionPanel({
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