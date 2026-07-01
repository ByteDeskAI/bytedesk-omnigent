import { useEffect, useMemo, useState } from "react";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  useCheckConnectorHealth,
  useGrantConnectorToAgent,
  useSetConnectorServiceEnabled,
} from "@/hooks/useConnectors";
import {
  groupConnectionServices,
  type AvailableConnectorTool,
} from "../connectors-utils";
import type { ConnectorPresentation } from "@/lib/connectorPresentation";
import type { ConnectionPanelProps } from "./types";

export type ConnectionPanelActionGroup = Omit<
  ConnectorPresentation["serviceGroups"][number],
  "services"
> & {
  services: Array<{
    definition: ConnectorPresentation["serviceGroups"][number]["services"][number];
    tools: AvailableConnectorTool[];
  }>;
};

export function useConnectionPanelState({
  connection,
  provider,
  presentation,
}: ConnectionPanelProps) {
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

  return {
    toggle,
    health,
    grant,
    agents,
    agentId,
    setAgentId,
    serviceGroups,
    actionGroups,
    availableTools,
    selectedTools,
    healthResult,
    requiredScopes,
    clientId,
    setToolSelected,
    setToolsSelected,
  };
}