import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import * as connectorHooks from "@/hooks/useConnectors";
import * as accountsApi from "@/lib/accountsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type {
  ConnectorConnection,
  ConnectorManifest,
  ConnectorServiceState,
} from "@/lib/connectorsApi";
import type { ServerInfo } from "@/lib/capabilities";
import { ConnectorDetailPage, ConnectorsPage } from "./ConnectorsPage";

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useConnectors", () => ({
  useConnectorsCatalog: vi.fn(),
  useStartConnectorOAuth: vi.fn(),
  useCreateConnectorConnection: vi.fn(),
  useSetConnectorServiceEnabled: vi.fn(),
  useCheckConnectorHealth: vi.fn(),
  useGrantConnectorToAgent: vi.fn(),
}));
vi.mock("@/lib/accountsApi", () => ({ getMe: vi.fn() }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));

const ACCOUNTS_OFF: ServerInfo = {
  accounts_enabled: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  omni_cli_terminal_enabled: true,
};

const ACCOUNTS_ON: ServerInfo = {
  ...ACCOUNTS_OFF,
  accounts_enabled: true,
  login_url: "/login",
};

const googleProvider: ConnectorManifest = {
  provider: "google_workspace",
  name: "Google Workspace",
  description: "Workspace connector",
  auth: {
    type: "google_domain_wide_delegation",
    scopes: [],
    docsUrl: "https://developers.google.com/workspace",
    setupFields: [
      {
        key: "delegated_subject",
        label: "Delegated subject",
        target: "metadata",
        input: "text",
        required: true,
      },
      {
        key: "service_account_email",
        label: "Service account email",
        target: "metadata",
        input: "text",
        required: false,
      },
      {
        key: "workload_identity_token_source",
        label: "Workload identity token source",
        target: "metadata",
        input: "text",
        required: false,
      },
      {
        key: "workload_identity_token_file",
        label: "Workload identity token file",
        target: "metadata",
        input: "text",
        required: false,
      },
      {
        key: "workload_identity_audience",
        label: "Workload identity audience",
        target: "metadata",
        input: "text",
        required: false,
      },
      {
        key: "service_account_json",
        label: "Service account JSON",
        target: "secret_payload",
        input: "json_secret",
        required: false,
      },
    ],
  },
  services: [
    {
      key: "drive",
      name: "Drive",
      description: "",
      scopes: [],
      toolMounts: [],
      tools: [
        {
          key: "search",
          name: "Search Drive",
          description: "",
          mcpTool: "drive_search",
          scopes: [],
        },
      ],
    },
    {
      key: "gmail",
      name: "Gmail",
      description: "",
      scopes: [],
      toolMounts: [],
      tools: [
        {
          key: "search",
          name: "Search Gmail",
          description: "",
          mcpTool: "gmail_search",
          scopes: [],
        },
      ],
    },
    {
      key: "calendar",
      name: "Calendar",
      description: "",
      scopes: [],
      toolMounts: [],
      tools: [
        {
          key: "event_create",
          name: "Create event",
          description: "",
          mcpTool: "calendar_event_create",
          scopes: [],
        },
      ],
    },
  ],
  connections: [],
};

const atlassianProvider: ConnectorManifest = {
  provider: "atlassian",
  name: "Atlassian",
  description: "Atlassian connector",
  auth: { type: "oauth_3lo", scopes: [], docsUrl: null, setupFields: [] },
  services: [
    {
      key: "jira",
      name: "Jira",
      description: "",
      scopes: [],
      toolMounts: [],
      tools: [
        {
          key: "search",
          name: "Search Jira",
          description: "",
          mcpTool: "jira_search",
          scopes: [],
        },
      ],
    },
  ],
  connections: [],
};

const createConnector = vi.fn();
const grantConnector = vi.fn();
const healthConnector = vi.fn();
const toggleConnector = vi.fn();
const startOAuth = vi.fn();

function googleServiceState(serviceKey: string): ConnectorServiceState {
  return {
    id: `svc_${serviceKey}`,
    connectionId: "conn_google",
    serviceKey,
    enabled: true,
    status: "ready",
    scopes: [],
    metadata: {},
    updatedAt: 1,
    version: 1,
  };
}

function googleConnection(overrides: Partial<ConnectorConnection> = {}): ConnectorConnection {
  return {
    id: "conn_google",
    provider: "google_workspace",
    displayName: "ByteDesk Workspace",
    authType: "google_domain_wide_delegation",
    status: "connected",
    scopes: [],
    metadata: {},
    secretPresent: true,
    lastHealthStatus: null,
    lastHealthAt: null,
    lastError: null,
    createdAt: 1,
    updatedAt: 1,
    version: 1,
    services: ["drive", "gmail", "calendar"].map(googleServiceState),
    grants: [],
    ...overrides,
  };
}

function mockCatalog(providers: ConnectorManifest[]) {
  vi.mocked(connectorHooks.useConnectorsCatalog).mockReturnValue({
    data: providers,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  } as never);
}

function renderCatalog() {
  return render(
    <MemoryRouter initialEntries={["/connectors"]}>
      <ConnectorsPage />
    </MemoryRouter>,
  );
}

function renderDetail(provider = "google_workspace") {
  return render(
    <MemoryRouter initialEntries={[`/connectors/${provider}`]}>
      <Routes>
        <Route path="/connectors" element={<ConnectorsPage />} />
        <Route path="/connectors/:provider" element={<ConnectorDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(useServerInfo).mockReturnValue(ACCOUNTS_OFF);
  vi.mocked(accountsApi.getMe).mockResolvedValue({
    id: "root",
    is_admin: true,
    created_at: null,
    last_login_at: null,
  });
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [{ id: "ag_maya", display_name: "Maya Chen" }],
  } as never);
  mockCatalog([atlassianProvider, googleProvider]);
  vi.mocked(connectorHooks.useStartConnectorOAuth).mockReturnValue({
    mutate: startOAuth,
    isPending: false,
    isError: false,
    error: null,
  } as never);
  createConnector.mockResolvedValue({});
  vi.mocked(connectorHooks.useCreateConnectorConnection).mockReturnValue({
    mutateAsync: createConnector,
    isPending: false,
    isError: false,
    error: null,
  } as never);
  vi.mocked(connectorHooks.useSetConnectorServiceEnabled).mockReturnValue({
    mutate: toggleConnector,
    isPending: false,
  } as never);
  vi.mocked(connectorHooks.useCheckConnectorHealth).mockReturnValue({
    mutate: healthConnector,
    isPending: false,
    data: undefined,
  } as never);
  vi.mocked(connectorHooks.useGrantConnectorToAgent).mockReturnValue({
    mutate: grantConnector,
    isPending: false,
    isError: false,
    error: null,
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ConnectorsPage", () => {
  it("renders a scannable provider catalog without setup fields or service pills", async () => {
    renderCatalog();

    expect(await screen.findByRole("heading", { name: "Connectors" })).toBeInTheDocument();
    expect(screen.getByText("Atlassian")).toBeInTheDocument();
    expect(screen.getByText("Google Workspace")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Delegated subject")).not.toBeInTheDocument();
    expect(screen.queryByText("Search Gmail")).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Configure" })).toHaveLength(2);
  });

  it("blocks non-admin accounts", async () => {
    vi.mocked(useServerInfo).mockReturnValue(ACCOUNTS_ON);
    vi.mocked(accountsApi.getMe).mockResolvedValue({
      id: "alice",
      is_admin: false,
      created_at: null,
      last_login_at: null,
    });

    renderCatalog();

    expect(
      await screen.findByText("You don't have permission to manage connectors."),
    ).toBeInTheDocument();
  });

  it("hides OAuth connect once a provider already has a connection", async () => {
    mockCatalog([
      { ...atlassianProvider, connections: [googleConnection({ provider: "atlassian" })] },
      googleProvider,
    ]);
    renderCatalog();

    await screen.findByText("Atlassian");

    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
  });

  it("keeps OAuth connect visible for disconnected OAuth providers", async () => {
    renderCatalog();

    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));

    expect(startOAuth).toHaveBeenCalledWith("atlassian");
  });

  it("links provider cards to the routed drilldown", async () => {
    renderCatalog();

    const links = await screen.findAllByRole("link", { name: "Configure" });
    expect(links.map((link) => link.getAttribute("href"))).toContain(
      "/connectors/google_workspace",
    );
  });
});

describe("ConnectorDetailPage", () => {
  it("renders breadcrumbs and grouped provider configuration", async () => {
    mockCatalog([{ ...googleProvider, connections: [googleConnection()] }, atlassianProvider]);

    renderDetail();

    expect(await screen.findByRole("link", { name: "Connectors" })).toHaveAttribute(
      "href",
      "/connectors",
    );
    expect(screen.getByRole("link", { name: /Back/ })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Connector provider" })).toHaveValue(
      "google_workspace",
    );
    expect(screen.getAllByText("Drive & Content").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Gmail").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Calendar & Meet").length).toBeGreaterThan(0);
  });

  it("submits direct credential providers through the generic create mutation", async () => {
    mockCatalog([googleProvider]);
    renderDetail();

    fireEvent.change(await screen.findByPlaceholderText("Delegated subject"), {
      target: { value: "admin@acme.test" },
    });
    fireEvent.change(screen.getByPlaceholderText("Service account JSON"), {
      target: { value: '{"client_email":"svc@acme.test"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() =>
      expect(createConnector).toHaveBeenCalledWith({
        provider: "google_workspace",
        displayName: "Google Workspace",
        metadata: { delegated_subject: "admin@acme.test" },
        secretPayload: {
          service_account_json: { client_email: "svc@acme.test" },
        },
        enabledServices: ["drive", "gmail", "calendar"],
      }),
    );
  });

  it("toggles grouped services through the existing mutation", async () => {
    mockCatalog([{ ...googleProvider, connections: [googleConnection()] }]);
    renderDetail();

    fireEvent.click(await screen.findByRole("switch", { name: "Toggle Drive" }));

    expect(toggleConnector).toHaveBeenCalledWith({
      connectionId: "conn_google",
      serviceKey: "drive",
      enabled: false,
    });
  });

  it("grants selected connector actions to Maya from grouped actions", async () => {
    mockCatalog([{ ...googleProvider, connections: [googleConnection()] }]);
    renderDetail();

    fireEvent.click(await screen.findByRole("checkbox", { name: /Search Gmail/ }));
    fireEvent.change(screen.getByLabelText("Agent"), {
      target: { value: "ag_maya" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Grant/ }));

    expect(grantConnector).toHaveBeenCalledWith({
      connectionId: "conn_google",
      agentId: "ag_maya",
      tools: ["drive:search", "calendar:event_create"],
    });
  });

  it("runs live Google health checks and shows DWD diagnostics", async () => {
    vi.mocked(connectorHooks.useCheckConnectorHealth).mockReturnValue({
      mutate: healthConnector,
      isPending: false,
      data: {
        ok: false,
        connection: googleConnection({
          secretPresent: false,
          lastHealthStatus: "error",
          lastError: "domain_wide_delegation_unauthorized",
          services: [],
        }),
        metadata: {
          clientId: "113703816904945094427",
          requiredScopes: ["https://www.googleapis.com/auth/drive"],
        },
      },
    } as never);
    mockCatalog([
      {
        ...googleProvider,
        connections: [
          googleConnection({
            secretPresent: false,
            lastHealthStatus: "error",
            lastError: "domain_wide_delegation_unauthorized",
            services: [],
          }),
        ],
      },
    ]);

    renderDetail();

    fireEvent.click(await screen.findByRole("button", { name: /Test/ }));

    expect(healthConnector).toHaveBeenCalledWith({
      connectionId: "conn_google",
      live: true,
    });
    expect(screen.getByText("domain_wide_delegation_unauthorized")).toBeInTheDocument();
    expect(screen.getByText(/113703816904945094427/)).toBeInTheDocument();
    expect(screen.getByText(/https:\/\/www.googleapis.com\/auth\/drive/)).toBeInTheDocument();
  });

  it("shows a not-found state for unknown providers", async () => {
    renderDetail("missing_provider");

    expect(await screen.findByRole("heading", { name: "Connector not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to connectors" })).toHaveAttribute(
      "href",
      "/connectors",
    );
  });

  it("renders a connection selector for providers with multiple connections", async () => {
    mockCatalog([
      {
        ...googleProvider,
        connections: [
          googleConnection(),
          googleConnection({ id: "conn_google_two", displayName: "Second Workspace" }),
        ],
      },
    ]);
    renderDetail();

    const selector = await screen.findByLabelText("Connection");
    expect(selector).toHaveValue("conn_google");
    fireEvent.change(selector, { target: { value: "conn_google_two" } });

    expect(screen.getByRole("heading", { name: "Second Workspace" })).toBeInTheDocument();
  });
});
