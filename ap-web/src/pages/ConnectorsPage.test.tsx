import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import * as connectorHooks from "@/hooks/useConnectors";
import * as accountsApi from "@/lib/accountsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { ConnectorManifest } from "@/lib/connectorsApi";
import type { ServerInfo } from "@/lib/capabilities";
import { ConnectorsPage } from "./ConnectorsPage";

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
    docsUrl: null,
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

function mockCatalog(providers: ConnectorManifest[]) {
  vi.mocked(connectorHooks.useConnectorsCatalog).mockReturnValue({
    data: providers,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  } as never);
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ConnectorsPage />
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
    mutate: vi.fn(),
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
    mutate: vi.fn(),
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
  it("renders registered providers and manifest setup fields in local mode", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Connectors" })).toBeInTheDocument();
    expect(screen.getByText("Atlassian")).toBeInTheDocument();
    expect(screen.getByText("Google Workspace")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Delegated subject")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Service account email")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Workload identity token source")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Workload identity token file")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Workload identity audience")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Service account JSON")).toBeInTheDocument();
  });

  it("blocks non-admin accounts", async () => {
    vi.mocked(useServerInfo).mockReturnValue(ACCOUNTS_ON);
    vi.mocked(accountsApi.getMe).mockResolvedValue({
      id: "alice",
      is_admin: false,
      created_at: null,
      last_login_at: null,
    });

    renderPage();

    expect(
      await screen.findByText("You don't have permission to manage connectors."),
    ).toBeInTheDocument();
  });

  it("submits direct credential providers through the generic create mutation", async () => {
    mockCatalog([googleProvider]);
    renderPage();

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
        enabledServices: ["drive", "gmail"],
      }),
    );
  });

  it("grants selected connector actions to Maya", async () => {
    mockCatalog([
      {
        ...googleProvider,
        connections: [
          {
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
              {
                id: "svc_gmail",
                connectionId: "conn_google",
                serviceKey: "gmail",
                enabled: true,
                status: "ready",
                scopes: [],
                metadata: {},
                updatedAt: 1,
                version: 1,
              },
            ],
            grants: [],
          },
        ],
      },
    ]);
    renderPage();

    fireEvent.click(await screen.findByRole("checkbox", { name: /Search Gmail/ }));
    fireEvent.change(screen.getByLabelText("Agent"), {
      target: { value: "ag_maya" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Grant/ }));

    expect(grantConnector).toHaveBeenCalledWith({
      connectionId: "conn_google",
      agentId: "ag_maya",
      tools: ["drive:search"],
    });
  });

  it("runs live Google health checks and shows DWD diagnostics", async () => {
    vi.mocked(connectorHooks.useCheckConnectorHealth).mockReturnValue({
      mutate: healthConnector,
      isPending: false,
      data: {
        ok: false,
        connection: {
          id: "conn_google",
          provider: "google_workspace",
          displayName: "ByteDesk Workspace",
          authType: "google_domain_wide_delegation",
          status: "connected",
          scopes: [],
          metadata: {},
          secretPresent: false,
          lastHealthStatus: "error",
          lastHealthAt: 1,
          lastError: "domain_wide_delegation_unauthorized",
          createdAt: 1,
          updatedAt: 1,
          version: 1,
          services: [],
          grants: [],
        },
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
          {
            id: "conn_google",
            provider: "google_workspace",
            displayName: "ByteDesk Workspace",
            authType: "google_domain_wide_delegation",
            status: "connected",
            scopes: [],
            metadata: {},
            secretPresent: false,
            lastHealthStatus: "error",
            lastHealthAt: 1,
            lastError: "domain_wide_delegation_unauthorized",
            createdAt: 1,
            updatedAt: 1,
            version: 1,
            services: [],
            grants: [],
          },
        ],
      },
    ]);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Test/ }));

    expect(healthConnector).toHaveBeenCalledWith({
      connectionId: "conn_google",
      live: true,
    });
    expect(screen.getByText("domain_wide_delegation_unauthorized")).toBeInTheDocument();
    expect(screen.getByText(/113703816904945094427/)).toBeInTheDocument();
    expect(screen.getByText(/https:\/\/www.googleapis.com\/auth\/drive/)).toBeInTheDocument();
  });
});
