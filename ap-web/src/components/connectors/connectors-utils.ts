import type { ConnectorConnection, ConnectorTool } from "@/lib/connectorsApi";
import type { ConnectorPresentation } from "@/lib/connectorPresentation";

export interface AvailableConnectorTool extends ConnectorTool {
  serviceKey: string;
  serviceName: string;
  token: string;
}

export function groupConnectionServices(
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