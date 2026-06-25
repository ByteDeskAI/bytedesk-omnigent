import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import * as goalHooks from "@/hooks/useGoals";
import { GoalsPage } from "./GoalsPage";

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useGoals", () => ({
  useGoals: vi.fn(),
  useGoalEvents: vi.fn(),
  useGoalPlannerSources: vi.fn(),
  useStartGoalPlanningSession: vi.fn(),
  useUpdateGoal: vi.fn(),
  useActivateGoal: vi.fn(),
  useAddGoalDependency: vi.fn(),
  useUpdateGoalDependency: vi.fn(),
}));

const startPlanningSession = vi.fn();
const updateGoal = vi.fn();
const activateGoal = vi.fn();
const addDependency = vi.fn();
const updateDependency = vi.fn();

function renderPage() {
  return render(
    <MemoryRouter>
      <GoalsPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [
      {
        id: "ag_maya",
        name: "chief-of-staff",
        display_name: "Maya Chen",
        description: null,
        harness: "codex",
        skills: [],
        department: "Operations",
        title: "Chief of Staff",
        workflow: false,
      },
      {
        id: "ag_growth",
        name: "marketing-director",
        display_name: "Claire Donovan",
        description: null,
        harness: "claude-sdk",
        skills: [],
        department: "Growth",
        title: "Growth Marketing Director",
        workflow: false,
      },
      {
        id: "ag_workflow",
        name: "weekly-business-review",
        display_name: "Weekly Business Review",
        description: null,
        harness: "claude-sdk",
        skills: [],
        department: "Operations",
        title: "Workflow",
        workflow: true,
      },
    ],
  } as never);
  vi.mocked(goalHooks.useGoals).mockReturnValue({
    data: [
      {
        id: "goal_1",
        title: "Prepare reporting loop",
        owner_agent_id: null,
        status: "open",
        priority: 2,
        source: "admin",
        payload: null,
        created_at: 100,
        updated_at: 110,
        target_kind: "department",
        target_id: "Operations",
        target_label: "Operations",
        readiness_kind: "dependent",
        activation_state: "waiting",
        dependencies: [
          {
            id: "goal_dep_1",
            goal_id: "goal_1",
            kind: "system_state",
            ref: null,
            label: "Warehouse export ready",
            status: "pending",
            created_at: 100,
            updated_at: 100,
            resolved_at: null,
            metadata: null,
          },
        ],
      },
    ],
    isFetching: false,
  } as never);
  vi.mocked(goalHooks.useGoalEvents).mockReturnValue(undefined);
  vi.mocked(goalHooks.useGoalPlannerSources).mockReturnValue({
    data: [
      { id: "jira", label: "Jira", available: true, tools: ["bytedesk_jira"] },
      {
        id: "confluence",
        label: "Confluence",
        available: true,
        tools: ["bytedesk_confluence"],
      },
      {
        id: "google_workspace",
        label: "Google Workspace",
        available: false,
        tools: [],
        reason: "not_configured",
      },
    ],
  } as never);
  startPlanningSession.mockResolvedValue({
    session_id: "conv_plan",
    agent_id: "ag_maya",
    agent_name: "chief-of-staff",
    title: "Plan goal: Organization",
    prompt: "GOAL PLANNING INTERVIEW",
    sources: [],
    web_path: "/c/conv_plan",
  });
  vi.mocked(goalHooks.useStartGoalPlanningSession).mockReturnValue({
    mutateAsync: startPlanningSession,
    isPending: false,
  } as never);
  vi.mocked(goalHooks.useUpdateGoal).mockReturnValue({
    mutateAsync: updateGoal,
    isPending: false,
  } as never);
  vi.mocked(goalHooks.useActivateGoal).mockReturnValue({
    mutateAsync: activateGoal,
    isPending: false,
  } as never);
  vi.mocked(goalHooks.useAddGoalDependency).mockReturnValue({
    mutateAsync: addDependency,
    isPending: false,
  } as never);
  vi.mocked(goalHooks.useUpdateGoalDependency).mockReturnValue({
    mutateAsync: updateDependency,
    isPending: false,
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GoalsPage", () => {
  it("renders scoped goals and resolves a pending dependency", async () => {
    renderPage();

    expect(screen.getByRole("heading", { name: "Goals" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Organization/ })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Department/ }).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /Employees/ })).toBeInTheDocument();
    expect(screen.getAllByText("Operations").length).toBeGreaterThan(0);
    expect(screen.queryByText("Weekly Business Review")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /OperationsDepartment goal/ }));
    fireEvent.click(screen.getByRole("button", { name: "Waiting" }));
    fireEvent.click(screen.getByRole("button", { name: /Prepare reporting loop/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Satisfy/ }));

    expect(updateDependency).toHaveBeenCalledWith({
      goalId: "goal_1",
      dependencyId: "goal_dep_1",
      payload: { status: "satisfied" },
    });
  });

  it("starts a planning interview for the selected scope", async () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /GrowthDepartment goal/ }));
    fireEvent.click(screen.getByRole("button", { name: /Start interview/ }));

    await waitFor(() =>
      expect(startPlanningSession).toHaveBeenCalledWith(
        expect.objectContaining({
          target_kind: "department",
          target_id: "Growth",
          target_label: "Growth",
          source_ids: ["jira", "confluence"],
        }),
      ),
    );
  });
});
