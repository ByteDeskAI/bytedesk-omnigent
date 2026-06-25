import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SkillsPage } from "./SkillsPage";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import * as skillsHooks from "@/hooks/useSkills";

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useSkills", () => ({
  useInstalledSkills: vi.fn(),
  useSkillSources: vi.fn(),
  useSkillMarketplaces: vi.fn(),
  useSkillRecommendations: vi.fn(),
  useStartSkillsConciergeSession: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: Object.assign(vi.fn(() => ({})), {
    getState: () => ({ switchTo: vi.fn() }),
  }),
}));
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
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: EMPLOYEES,
    isLoading: false,
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
  it("renders the shell, scope selector, conversation, and installed rail", async () => {
    renderPage();

    expect(screen.getByRole("heading", { name: "Skills" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Organizational/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Operations/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Engineering/ })).toBeInTheDocument();
    expect(screen.getByTestId("agent-conversation")).toBeInTheDocument();
    expect(screen.getByText("ByteDesk Catalog")).toBeInTheDocument();
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
