import type { ConnectorManifest, ConnectorService } from "@/lib/connectorsApi";

export interface ConnectorSummary {
  connectionCount: number;
  serviceCount: number;
  actionCount: number;
  enabledServiceCount: number;
  grantCount: number;
  status: string;
}

export interface ConnectorServiceGroup {
  key: string;
  name: string;
  services: ConnectorService[];
  serviceCount: number;
  actionCount: number;
}

export interface ConnectorPresentation {
  summary: ConnectorSummary;
  serviceGroups: ConnectorServiceGroup[];
}

interface GroupDefinition {
  key: string;
  name: string;
  services: string[];
}

const GOOGLE_WORKSPACE_GROUPS: GroupDefinition[] = [
  { key: "workspace", name: "Workspace", services: ["workspace"] },
  { key: "gmail", name: "Gmail", services: ["gmail", "gmail_settings", "postmaster_tools"] },
  { key: "calendar", name: "Calendar & Meet", services: ["calendar", "meet"] },
  { key: "chat", name: "Chat & Tasks", services: ["chat", "tasks", "keep"] },
  {
    key: "drive-content",
    name: "Drive & Content",
    services: [
      "drive",
      "docs",
      "sheets",
      "slides",
      "forms",
      "sites",
      "cloud_search",
      "drive_activity",
      "drive_labels",
      "drive_apps",
    ],
  },
  {
    key: "people",
    name: "People & Contacts",
    services: ["people", "domain_shared_contacts", "contact_delegation"],
  },
  {
    key: "admin-directory",
    name: "Admin & Directory",
    services: [
      "admin_settings",
      "admin_directory",
      "cloud_identity",
      "groups_settings",
      "groups_migration",
      "license_manager",
      "data_transfer",
      "reseller",
      "marketplace",
    ],
  },
  {
    key: "chrome-devices",
    name: "Chrome & Devices",
    services: [
      "chrome_browser_cloud_management",
      "chrome_enrollment_tokens",
      "chrome_printer_management",
    ],
  },
  {
    key: "compliance",
    name: "Compliance",
    services: ["reports", "alert_center", "email_audit", "vault"],
  },
  {
    key: "automation-ai",
    name: "Automation & AI",
    services: ["apps_script", "workspace_add_ons", "vertex_ai"],
  },
];

const ATLASSIAN_GROUPS: GroupDefinition[] = [
  { key: "jira", name: "Jira", services: ["jira"] },
  { key: "confluence", name: "Confluence", services: ["confluence"] },
];

function groupDefinitionsForProvider(provider: string): GroupDefinition[] {
  if (provider === "google_workspace") return GOOGLE_WORKSPACE_GROUPS;
  if (provider === "atlassian") return ATLASSIAN_GROUPS;
  return [];
}

function summarizeConnector(provider: ConnectorManifest): ConnectorSummary {
  const enabledServiceCount = provider.connections.reduce(
    (count, connection) => count + connection.services.filter((service) => service.enabled).length,
    0,
  );
  const grantCount = provider.connections.reduce(
    (count, connection) => count + connection.grants.length,
    0,
  );
  const unhealthy = provider.connections.find(
    (connection) =>
      connection.lastHealthStatus === "error" ||
      connection.lastHealthStatus === "unhealthy" ||
      connection.status === "error",
  );
  return {
    connectionCount: provider.connections.length,
    serviceCount: provider.services.length,
    actionCount: provider.services.reduce((count, service) => count + service.tools.length, 0),
    enabledServiceCount,
    grantCount,
    status: unhealthy
      ? (unhealthy.lastHealthStatus ?? unhealthy.status)
      : provider.connections.length > 0
        ? "connected"
        : "not connected",
  };
}

export function buildConnectorPresentation(provider: ConnectorManifest): ConnectorPresentation {
  const servicesByKey = new Map(provider.services.map((service) => [service.key, service]));
  const assigned = new Set<string>();
  const serviceGroups: ConnectorServiceGroup[] = [];

  for (const definition of groupDefinitionsForProvider(provider.provider)) {
    const services = definition.services.flatMap((serviceKey) => {
      const service = servicesByKey.get(serviceKey);
      if (!service) return [];
      assigned.add(serviceKey);
      return [service];
    });
    if (services.length === 0) continue;
    serviceGroups.push({
      key: definition.key,
      name: definition.name,
      services,
      serviceCount: services.length,
      actionCount: services.reduce((count, service) => count + service.tools.length, 0),
    });
  }

  const otherServices = provider.services.filter((service) => !assigned.has(service.key));
  if (otherServices.length > 0) {
    serviceGroups.push({
      key: "other",
      name: groupDefinitionsForProvider(provider.provider).length > 0 ? "Other services" : "Services",
      services: otherServices,
      serviceCount: otherServices.length,
      actionCount: otherServices.reduce((count, service) => count + service.tools.length, 0),
    });
  }

  return {
    summary: summarizeConnector(provider),
    serviceGroups,
  };
}
