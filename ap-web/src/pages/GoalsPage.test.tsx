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
  useCreateGoal: vi.fn(),
  useUpdateGoal: vi.fn(),
  useActivateGoal: vi.fn(),
  useAddGoalDependency: vi.fn(),
  useUpdateGoalDependency: vi.fn(),
}));

const createGoal = vi.fn();
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
  createGoal.mockResolvedValue({
    id: "goal_new",
    activation_state: "waiting",
  });
  vi.mocked(goalHooks.useCreateGoal).mockReturnValue({
    mutateAsync: createGoal,
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
    expect(screen.getAllByText("Operations").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Waiting" }));
    fireEvent.click(screen.getByRole("button", { name: /Prepare reporting loop/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Satisfy/ }));

    expect(updateDependency).toHaveBeenCalledWith({
      goalId: "goal_1",
      dependencyId: "goal_dep_1",
      payload: { status: "satisfied" },
    });
  });

  it("creates a dependent organization goal from the editor", async () => {
    renderPage();

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Close onboarding loop" },
    });
    fireEvent.change(screen.getByLabelText("Dependencies"), {
      target: { value: "Agent roster synced\nDefault policy published" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create goal/ }));

    await waitFor(() =>
      expect(createGoal).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Close onboarding loop",
          target_kind: "organization",
          target_id: "omnigent",
          readiness_kind: "dependent",
          dependencies: [
            { kind: "manual", label: "Agent roster synced", status: "pending" },
            { kind: "manual", label: "Default policy published", status: "pending" },
          ],
        }),
      ),
    );
  });
});
