import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkForcePage } from "./WorkForcePage";
import * as imageHooks from "@/hooks/useAgentImages";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import * as connectorHooks from "@/hooks/useConnectors";
import * as skillsHooks from "@/hooks/useSkills";
import * as accountsApi from "@/lib/accountsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";

vi.mock("@/hooks/useAgentImages", () => ({
  useAgentImage: vi.fn(),
  useAgentImageTree: vi.fn(),
  useReadAgentImageFile: vi.fn(),
  useUpdateAgentImage: vi.fn(),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useConnectors", () => ({
  useConnectorsCatalog: vi.fn(),
  useConnectorAgentGrants: vi.fn(),
  useGrantConnectorToAgent: vi.fn(),
}));
vi.mock("@/hooks/useSkills", () => ({
  useInstalledSkills: vi.fn(),
  useSearchSkills: vi.fn(),
  useCreateSkillPreview: vi.fn(),
  useApplySkillPreview: vi.fn(),
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

const agents = [
  {
    id: "ag_employee",
    name: "platform-developer",
    display_name: "Platform Developer",
    description: "Builds platform code.",
    harness: "codex",
    skills: [],
    department: "Engineering",
    title: "Platform Engineer",
    workflow: false,
    category: "employee",
  },
  {
    id: "ag_system",
    name: "polly",
    display_name: "Polly",
    description: "System router.",
    harness: "claude-sdk",
    skills: [],
    department: null,
    title: null,
    workflow: false,
    category: "system",
  },
  {
    id: "ag_workflow",
    name: "weekly-business-review",
    display_name: "Weekly Business Review",
    description: "Weekly workflow.",
    harness: "claude-sdk",
    skills: [],
    department: "Operations",
    title: "Workflow",
    workflow: true,
    category: "workflow",
  },
  {
    id: "ag_demo",
    name: "inbox-demo",
    display_name: "Inbox Demo",
    description: "Reference workflow sample.",
    harness: "claude-sdk",
    skills: [],
    department: null,
    title: null,
    workflow: false,
    category: "employee",
  },
];

const mutateImage = vi.fn();

function renderPage() {
  return render(
    <MemoryRouter>
      <WorkForcePage />
    </MemoryRouter>,
  );
}

async function activateTab(name: RegExp) {
  const tab = await screen.findByRole("tab", { name });
  fireEvent.mouseDown(tab, { button: 0, ctrlKey: false });
  fireEvent.mouseUp(tab, { button: 0 });
  fireEvent.click(tab);
  await waitFor(() => expect(tab).toHaveAttribute("aria-selected", "true"));
  return tab;
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
    data: agents,
    isLoading: false,
    refetch: vi.fn(),
  } as never);
  vi.mocked(imageHooks.useAgentImage).mockImplementation((agentId) => {
    if (!agentId) {
      return { data: undefined, isError: false, error: null, refetch: vi.fn() } as never;
    }
    return {
      data: {
        image: {
          id: agentId,
          name: String(agentId),
          version: 3,
          config: {
            spec_version: 1,
            name: "platform-developer",
            executor: { type: "omnigent", config: { harness: "codex" } },
          },
          instructions: "Use the repo rules.\n",
          skills: [],
          mcp_servers: [],
          python_tools: [],
          typescript_tools: [],
          sub_agents: [],
          sot_tier: "migrated",
        },
        etag: '"3"',
      },
      isError: false,
      error: null,
      refetch: vi.fn(),
    } as never;
  });
  vi.mocked(imageHooks.useAgentImageTree).mockReturnValue({
    data: { id: "ag_employee", name: "platform-developer", version: 3, path: ".", entries: [] },
    isError: false,
    error: null,
  } as never);
  vi.mocked(imageHooks.useReadAgentImageFile).mockReturnValue({
    mutateAsync: vi.fn(),
  } as never);
  mutateImage.mockResolvedValue({});
  vi.mocked(imageHooks.useUpdateAgentImage).mockReturnValue({
    mutateAsync: mutateImage,
    isPending: false,
  } as never);
  vi.mocked(connectorHooks.useConnectorsCatalog).mockReturnValue({
    data: [],
    isLoading: false,
  } as never);
  vi.mocked(connectorHooks.useConnectorAgentGrants).mockReturnValue({
    data: [],
    isLoading: false,
  } as never);
  vi.mocked(connectorHooks.useGrantConnectorToAgent).mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  } as never);
  vi.mocked(skillsHooks.useInstalledSkills).mockReturnValue({
    data: [],
    isLoading: false,
  } as never);
  vi.mocked(skillsHooks.useSearchSkills).mockReturnValue({
    mutateAsync: vi.fn(),
    isPending: false,
  } as never);
  vi.mocked(skillsHooks.useCreateSkillPreview).mockReturnValue({
    mutateAsync: vi.fn(),
    isPending: false,
  } as never);
  vi.mocked(skillsHooks.useApplySkillPreview).mockReturnValue({
    mutateAsync: vi.fn(),
    isPending: false,
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WorkForcePage", () => {
  it("groups employees, system agents, and workflows separately", async () => {
    renderPage();

    expect(vi.mocked(useAvailableAgents)).toHaveBeenCalledWith({ includeSessionAgents: false });
    expect(await screen.findByRole("heading", { name: "Work Force" })).toBeInTheDocument();
    expect(screen.getAllByText("Employees").length).toBeGreaterThan(0);
    expect(screen.getAllByText("System Agents").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Workflows").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Platform Developer").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Polly").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Weekly Business Review").length).toBeGreaterThan(0);
    expect(within(screen.getByLabelText("Agent roster")).queryByText("Inbox Demo")).toBeNull();
  });

  it("groups workforce employees by sorted department and employee name", async () => {
    vi.mocked(useAvailableAgents).mockReturnValue({
      data: [
        {
          id: "ag_marketing",
          name: "brand-lead",
          display_name: "Brand Lead",
          description: null,
          harness: "claude-sdk",
          skills: [],
          department: "Marketing",
          title: "Brand Lead",
          workflow: false,
          category: "employee",
        },
        {
          id: "ag_platform",
          name: "platform-developer",
          display_name: "Platform Developer",
          description: null,
          harness: "codex",
          skills: [],
          department: "Engineering",
          title: "Platform Engineer",
          workflow: false,
          category: "employee",
        },
        {
          id: "ag_backend",
          name: "backend-lead",
          display_name: "Backend Lead",
          description: null,
          harness: "codex",
          skills: [],
          department: "Engineering",
          title: "Backend Lead",
          workflow: false,
          category: "employee",
        },
        {
          id: "ag_hello",
          name: "hello_world",
          display_name: "Hello World",
          description: null,
          harness: "openai-agents",
          skills: [],
          department: null,
          title: null,
          workflow: false,
          category: "employee",
        },
        {
          id: "ag_goal",
          name: "goal-commander",
          display_name: "Goal Commander",
          description: null,
          harness: "claude-sdk",
          skills: [],
          department: "Operations",
          title: "Goals Command Center Operator",
          workflow: false,
          category: "system",
        },
      ],
      isLoading: false,
      refetch: vi.fn(),
    } as never);

    renderPage();

    const roster = await screen.findByLabelText("Agent roster");
    const engineering = within(roster).getByRole("button", { name: /Department Engineering/ });
    const marketing = within(roster).getByRole("button", { name: /Department Marketing/ });
    expect(engineering.compareDocumentPosition(marketing) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );

    const backend = within(roster).getAllByText("Backend Lead")[0];
    const platform = within(roster).getByText("Platform Developer");
    expect(backend.compareDocumentPosition(platform) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(within(roster).queryByText("Hello World")).toBeNull();
    expect(within(roster).getByText("Goal Commander")).toBeInTheDocument();
  });

  it("keeps workflow agents read-only", async () => {
    renderPage();

    fireEvent.click(await screen.findByText("Weekly Business Review"));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Weekly Business Review" })).toBeInTheDocument(),
    );
    expect(screen.getAllByText("Read-only").length).toBeGreaterThan(0);
    expect(screen.getByRole("tab", { name: /Config/ })).toBeDisabled();
  });

  it("requires confirmation before saving a system agent image", async () => {
    renderPage();

    fireEvent.click(await screen.findByText("Polly"));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Polly" })).toBeInTheDocument());
    const configTab = screen.getByRole("tab", { name: /Config/ });
    await waitFor(() => expect(configTab).not.toBeDisabled());
    await activateTab(/Config/);
    fireEvent.click(await screen.findByRole("button", { name: /Save image/ }));

    expect(
      await screen.findByRole("heading", { name: "Confirm system agent edit" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save system agent" }));

    await waitFor(() => expect(mutateImage).toHaveBeenCalled());
    expect(mutateImage.mock.calls[0][0]).toMatchObject({
      agentId: "ag_system",
      etag: '"3"',
    });
  });

  it("scopes skills and connector grants to the selected employee", async () => {
    renderPage();

    await activateTab(/Skills/);
    await waitFor(() =>
      expect(vi.mocked(skillsHooks.useInstalledSkills).mock.calls).toContainEqual(["ag_employee"]),
    );

    await activateTab(/Connectors/);
    await waitFor(() =>
      expect(vi.mocked(connectorHooks.useConnectorAgentGrants).mock.calls).toContainEqual([
        "ag_employee",
      ]),
    );
  });
});
