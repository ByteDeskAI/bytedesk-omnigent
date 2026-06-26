import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SkillsPage } from "./SkillsPage";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useHosts } from "@/hooks/useHosts";
import * as skillsHooks from "@/hooks/useSkills";

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({ useHostFilesystem: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useSkills", () => ({
  useInstalledSkills: vi.fn(),
  useSkillSources: vi.fn(),
  useSkillMarketplaces: vi.fn(),
  useSkillRecommendations: vi.fn(),
  useStartSkillsConciergeSession: vi.fn(),
  useSearchSkills: vi.fn(),
  useCreateSkillPreview: vi.fn(),
  useApplySkillPreview: vi.fn(),
}));
const { switchTo } = vi.hoisted(() => ({ switchTo: vi.fn() }));
vi.mock("@/store/chatStore", () => ({
  useChatStore: Object.assign(vi.fn(() => ({})), {
    getState: () => ({ conversationId: "conv_prev", switchTo }),
  }),
}));
const { bindOnlyOnlineRunner, launchRunner } = vi.hoisted(() => ({
  bindOnlyOnlineRunner: vi.fn(),
  launchRunner: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", () => ({ bindOnlyOnlineRunner, launchRunner }));
// The embedded chat atoms subscribe to the module-level chatStore; stub
// them so the page test stays a focused structural render.
vi.mock("@/components/chat", () => ({
  AgentConversation: () => <div data-testid="agent-conversation" />,
  AgentComposer: ({ agentId }: { agentId: string }) => (
    <div data-testid="agent-composer">{agentId}</div>
  ),
}));

function renderPage() {
  return render(
    <MemoryRouter>
      <SkillsPage />
    </MemoryRouter>,
  );
}

const EMPLOYEES = [
  {
    // Live shape: a seeded built-in carries a generated `ag_…` id and a null
    // display_name; `name` is the stable handle the page resolves on.
    id: "ag_d5a1b59732f6819ed9b38132d6170412",
    name: "skills-concierge",
    display_name: null,
    description: null,
    harness: "claude-sdk",
    skills: [],
    department: "Operations",
    title: "Concierge",
    workflow: false,
  },
  {
    id: "ag_helper",
    name: "helper",
    display_name: "Helper",
    description: null,
    harness: "claude-sdk",
    skills: [],
    department: "Engineering",
    title: "Helper",
    workflow: false,
  },
];

beforeEach(() => {
  bindOnlyOnlineRunner.mockResolvedValue({ id: "conv_concierge" });
  launchRunner.mockResolvedValue({ runnerId: "runner_1" });
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: EMPLOYEES,
    isLoading: false,
  } as never);
  vi.mocked(useHosts).mockReturnValue({
    data: [{ host_id: "host_1", name: "dev", owner: "me", status: "online" }],
  } as never);
  vi.mocked(useHostFilesystem).mockReturnValue({
    data: {
      entries: [
        {
          name: "project",
          path: "/home/me/project",
          type: "directory",
          bytes: null,
          modified_at: 1,
        },
      ],
      truncated: false,
    },
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
  vi.mocked(skillsHooks.useStartSkillsConciergeSession).mockReturnValue({
    mutateAsync: vi.fn(),
  } as never);
  vi.mocked(skillsHooks.useSkillMarketplaces).mockReturnValue({
    data: [{ id: "github:ByteDeskAI-bytedesk-marketplace", label: "ByteDesk Catalog", source_id: "github_marketplace", kind: "github_catalog", description: null, default: true, repo: "ByteDeskAI/bytedesk-marketplace" }],
    isLoading: false,
  } as never);
  vi.mocked(skillsHooks.useSkillRecommendations).mockReturnValue({
    data: [{ name: "platform-dev", source: "github_marketplace", source_ref: "ByteDeskAI/bytedesk-marketplace@platform-dev", reason: "Suggested" }],
    isLoading: false,
  } as never);
  vi.mocked(skillsHooks.useInstalledSkills).mockReturnValue({
    data: [
      {
        name: "deep-search",
        description: "Search stuff.",
        agents: [
          { id: "ag_d5a1b59732f6819ed9b38132d6170412", name: "skills-concierge", version: 1 },
        ],
      },
    ],
    isLoading: false,
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SkillsPage", () => {
  it("starts a fresh concierge chat on open and restores the prior conversation on close", () => {
    // On open: stash the caller's active conversation and reset to a fresh chat
    // so the composer's first send binds to the concierge — not whatever agent
    // (e.g. Maya) the user last chatted with in the shared global chat store.
    const { unmount } = renderPage();
    expect(switchTo).toHaveBeenCalledWith(null);
    // On close: restore the caller's previous conversation.
    unmount();
    expect(switchTo).toHaveBeenCalledWith("conv_prev");
  });

  it("binds an online runner to the concierge session the backend returns", async () => {
    // When POST /v1/skills/concierge/sessions returns a session, the panel
    // switches to it AND binds a runner (the backend creates the session but
    // leaves runner_id unset) so the concierge can answer on the first send.
    vi.mocked(skillsHooks.useStartSkillsConciergeSession).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({
        session_id: "conv_concierge",
        agent_id: "ag_d5a1b59732f6819ed9b38132d6170412",
        agent_name: "skills-concierge",
        title: "Skills",
        prompt: "",
        web_path: "/c/conv_concierge",
      }),
    } as never);

    renderPage();

    await vi.waitFor(() => {
      expect(switchTo).toHaveBeenCalledWith("conv_concierge");
      expect(bindOnlyOnlineRunner).toHaveBeenCalledWith("conv_concierge");
    });
    expect(launchRunner).not.toHaveBeenCalled();
  });

  it("launches the concierge session on an online host when no runner is already online", async () => {
    bindOnlyOnlineRunner.mockResolvedValue(null);
    vi.mocked(skillsHooks.useStartSkillsConciergeSession).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({
        session_id: "conv_concierge",
        agent_id: "ag_d5a1b59732f6819ed9b38132d6170412",
        agent_name: "skills-concierge",
        title: "Skills",
        prompt: "",
        web_path: "/c/conv_concierge",
      }),
    } as never);

    renderPage();

    await vi.waitFor(() => {
      expect(launchRunner).toHaveBeenCalledWith("host_1", "conv_concierge", "/home/me");
    });
  });

  it("renders the shell, scope selector, conversation, and installed rail", async () => {
    renderPage();

    expect(screen.getByRole("heading", { name: "Skills" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Organizational/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Operations/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Engineering/ })).toBeInTheDocument();
    expect(screen.getByTestId("agent-conversation")).toBeInTheDocument();
    // The Catalog section header is a generic title; "ByteDesk Catalog" is the
    // marketplace chip below it — guards against the header re-duplicating the
    // marketplace name (which made getByText("ByteDesk Catalog") ambiguous).
    expect(screen.getByRole("heading", { name: "Catalog" })).toBeInTheDocument();
    expect(screen.getByText("ByteDesk Catalog")).toBeInTheDocument();
    // The source badge shows a friendly label, not the raw source_id.
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    expect(screen.queryByText("github_marketplace")).toBeNull();
    expect(screen.getByLabelText("Search ByteDesk catalog")).toBeInTheDocument();
    expect(screen.getByText("platform-dev")).toBeInTheDocument();
    expect(screen.getByText("Installed Skills")).toBeInTheDocument();
    expect(screen.getByText("deep-search")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Close skills/ })).toBeInTheDocument();
  });

  it("orders departments by name and employees by department then name", () => {
    const mk = (id: string, display_name: string, department: string, workflow = false) => ({
      id,
      name: id,
      display_name,
      department,
      title: null,
      description: null,
      harness: "claude-sdk",
      skills: [],
      workflow,
    });
    vi.mocked(useAvailableAgents).mockReturnValue({
      data: [
        mk("a_zara", "Zara", "Sales"),
        mk("a_bob", "Bob", "Engineering"),
        mk("a_alice", "Alice", "Engineering"),
        mk("a_mona", "Mona", "Marketing"),
        mk("a_router", "Router", "Engineering", true), // workflow → excluded
        mk("ag_cc", "Concierge", "Operations"),
        { ...mk("ag_cc", "Concierge", "Operations"), name: "skills-concierge" },
      ],
      isLoading: false,
    } as never);

    renderPage();
    const labels = screen
      .getAllByRole("button")
      .map((b) => b.getAttribute("aria-label") || b.textContent || "");
    const order = (s: string) => labels.findIndex((l) => l.includes(s));

    // Departments sorted by name (case-insensitive).
    expect(order("Engineering")).toBeLessThan(order("Marketing"));
    expect(order("Marketing")).toBeLessThan(order("Operations"));
    expect(order("Operations")).toBeLessThan(order("Sales"));

    // Employees ordered by department then name: Engineering's Alice before Bob,
    // both before Marketing's Mona.
    expect(order("Alice")).toBeLessThan(order("Bob"));
    expect(order("Bob")).toBeLessThan(order("Mona"));
  });

  it("routes the composer to the concierge by name, using its generated id", () => {
    renderPage();
    // Resolved by name === "skills-concierge"; the composer is wired to the
    // agent's actual (generated) id, not the name.
    expect(screen.getByTestId("agent-composer")).toHaveTextContent(
      "ag_d5a1b59732f6819ed9b38132d6170412",
    );
  });

  it("falls back to a display-name match for the concierge", () => {
    vi.mocked(useAvailableAgents).mockReturnValue({
      data: [
        {
          id: "ag_other",
          name: "skills_concierge_bot",
          display_name: "Skills Concierge Bot",
          description: null,
          harness: "codex",
          skills: [],
          department: "Operations",
          title: "Concierge",
          workflow: false,
        },
      ],
      isLoading: false,
    } as never);
    renderPage();
    expect(screen.getByTestId("agent-composer")).toHaveTextContent("ag_other");
  });

  it("shows an unavailable notice when no concierge agent exists", () => {
    vi.mocked(useAvailableAgents).mockReturnValue({
      data: [
        {
          id: "ag_helper",
          name: "helper",
          display_name: "Helper",
          description: null,
          harness: "codex",
          skills: [],
          department: "Engineering",
          title: "Helper",
          workflow: false,
        },
      ],
      isLoading: false,
    } as never);
    renderPage();
    expect(screen.getByText(/Skills assistant isn't available yet/)).toBeInTheDocument();
    expect(screen.queryByTestId("agent-composer")).toBeNull();
  });
});
