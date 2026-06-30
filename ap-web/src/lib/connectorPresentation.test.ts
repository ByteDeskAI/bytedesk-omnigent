import { describe, expect, it } from "vitest";
import type { ConnectorManifest, ConnectorService } from "@/lib/connectorsApi";
import { buildConnectorPresentation } from "./connectorPresentation";

const GOOGLE_SERVICE_KEYS = [
  "workspace",
  "gmail",
  "calendar",
  "chat",
  "drive",
  "docs",
  "sheets",
  "slides",
  "forms",
  "keep",
  "meet",
  "sites",
  "tasks",
  "admin_settings",
  "admin_directory",
  "cloud_identity",
  "people",
  "domain_shared_contacts",
  "contact_delegation",
  "groups_settings",
  "groups_migration",
  "license_manager",
  "reports",
  "alert_center",
  "data_transfer",
  "reseller",
  "cloud_search",
  "drive_activity",
  "drive_labels",
  "apps_script",
  "workspace_add_ons",
  "drive_apps",
  "marketplace",
  "gmail_settings",
  "email_audit",
  "postmaster_tools",
  "chrome_browser_cloud_management",
  "chrome_enrollment_tokens",
  "chrome_printer_management",
  "vault",
  "vertex_ai",
];

function service(key: string): ConnectorService {
  return {
    key,
    name: key,
    description: "",
    scopes: [],
    toolMounts: [],
    tools: [
      {
        key: "read",
        name: `Read ${key}`,
        description: "",
        mcpTool: `${key}_read`,
        scopes: [],
      },
    ],
  };
}

function provider(overrides: Partial<ConnectorManifest> = {}): ConnectorManifest {
  return {
    provider: "google_workspace",
    name: "Google Workspace",
    description: "",
    auth: { type: "google_domain_wide_delegation", scopes: [], docsUrl: null, setupFields: [] },
    services: GOOGLE_SERVICE_KEYS.map(service),
    connections: [],
    ...overrides,
  };
}

describe("buildConnectorPresentation", () => {
  it("groups every known Google Workspace service exactly once", () => {
    const presentation = buildConnectorPresentation(provider());
    const groupedKeys = presentation.serviceGroups.flatMap((group) =>
      group.services.map((item) => item.key),
    );

    expect(groupedKeys).toHaveLength(GOOGLE_SERVICE_KEYS.length);
    expect(new Set(groupedKeys)).toEqual(new Set(GOOGLE_SERVICE_KEYS));
    expect(presentation.serviceGroups.map((group) => group.name)).toContain("Drive & Content");
    expect(presentation.serviceGroups.map((group) => group.name)).toContain("Admin & Directory");
  });

  it("puts unmapped services in Other services", () => {
    const presentation = buildConnectorPresentation(
      provider({ services: [...GOOGLE_SERVICE_KEYS.map(service), service("custom_service")] }),
    );

    const other = presentation.serviceGroups.find((group) => group.key === "other");
    expect(other?.name).toBe("Other services");
    expect(other?.services.map((item) => item.key)).toEqual(["custom_service"]);
  });

  it("summarizes services, actions, connections, and grants", () => {
    const presentation = buildConnectorPresentation(
      provider({
        services: [
          { ...service("drive"), tools: [service("drive").tools[0], service("drive").tools[0]] },
          service("gmail"),
        ],
        connections: [
          {
            id: "conn_google",
            provider: "google_workspace",
            displayName: "ByteDesk",
            authType: "google_domain_wide_delegation",
            status: "connected",
            scopes: [],
            metadata: {},
            secretPresent: true,
            lastHealthStatus: "healthy",
            lastHealthAt: 1,
            lastError: null,
            createdAt: 1,
            updatedAt: 1,
            version: 1,
            services: [
              {
                id: "svc_drive",
                connectionId: "conn_google",
                serviceKey: "drive",
                enabled: true,
                status: "ready",
                scopes: [],
                metadata: {},
                updatedAt: 1,
                version: 1,
              },
            ],
            grants: [
              {
                id: "grant_drive",
                connectionId: "conn_google",
                agentId: "ag_maya",
                serviceKey: "drive",
                toolKey: "read",
                enabled: true,
                status: "active",
                metadata: {},
                createdAt: 1,
                updatedAt: 1,
                version: 1,
              },
            ],
          },
        ],
      }),
    );

    expect(presentation.summary).toMatchObject({
      connectionCount: 1,
      serviceCount: 2,
      actionCount: 3,
      enabledServiceCount: 1,
      grantCount: 1,
      status: "connected",
    });
  });
});
