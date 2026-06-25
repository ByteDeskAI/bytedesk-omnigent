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
    id: "skills-concierge",
    name: "skills-concierge",
    display_name: "Skills Concierge",
    description: null,
    harness: "codex",
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
  vi.mocked(skillsHooks.useInstalledSkills).mockReturnValue({
    data: [
      {
        name: "deep-search",
        description: "Search stuff.",
        agents: [{ id: "skills-concierge", name: "skills-concierge", version: 1 }],
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
    expect(screen.getByText("deep-search")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Close skills/ })).toBeInTheDocument();
  });

  it("routes the composer to the skills-concierge agent", () => {
    renderPage();
    expect(screen.getByTestId("agent-composer")).toHaveTextContent("skills-concierge");
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
