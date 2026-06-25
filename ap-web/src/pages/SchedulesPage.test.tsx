import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import * as scheduleHooks from "@/hooks/useSchedules";
import { SchedulesPage } from "./SchedulesPage";

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useSchedules", () => ({
  useTaskTemplates: vi.fn(),
  useSchedules: vi.fn(),
  useScheduleOccurrences: vi.fn(),
  useCreateTaskTemplate: vi.fn(),
  useCreateSchedule: vi.fn(),
  useDraftCadence: vi.fn(),
}));

const createSchedule = vi.fn();
const createTask = vi.fn();
const draftCadence = vi.fn();

function renderPage() {
  return render(
    <MemoryRouter>
      <SchedulesPage />
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
        id: "ag_wbr",
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
  vi.mocked(scheduleHooks.useTaskTemplates).mockReturnValue({
    data: [
      {
        id: "task_wf_weekly-business-review",
        title: "Run the weekly business review",
        owner_agent_id: "weekly-business-review",
        assignee_agent_id: "weekly-business-review",
        required_capability: "operations",
        status: "open",
        priority: 3,
        source: "workflow-bundle",
        payload: { prompt: "Run the weekly review." },
        created_at: 1,
        updated_at: 1,
      },
    ],
    isLoading: false,
  } as never);
  vi.mocked(scheduleHooks.useSchedules).mockReturnValue({
    data: [{ id: "cron_1" }],
    isLoading: false,
  } as never);
  vi.mocked(scheduleHooks.useScheduleOccurrences).mockReturnValue({
    data: [
      {
        id: "cron_1:1782396000",
        schedule_id: "cron_1",
        agent_id: "ag_maya",
        task_id: "task_wf_weekly-business-review",
        title: "Run the weekly business review",
        fire_at: 1782396000,
      },
    ],
    isLoading: false,
  } as never);
  createTask.mockResolvedValue({ id: "task_new" });
  createSchedule.mockResolvedValue({ id: "cron_new" });
  draftCadence.mockResolvedValue({ schedule_kind: "cron", schedule_expr: "30 8 * * 1-5" });
  vi.mocked(scheduleHooks.useCreateTaskTemplate).mockReturnValue({
    mutateAsync: createTask,
    isPending: false,
  } as never);
  vi.mocked(scheduleHooks.useCreateSchedule).mockReturnValue({
    mutateAsync: createSchedule,
    isPending: false,
  } as never);
  vi.mocked(scheduleHooks.useDraftCadence).mockReturnValue({
    mutateAsync: draftCadence,
    isPending: false,
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SchedulesPage", () => {
  it("opens the create surface and submits a schedule for the selected agent", async () => {
    renderPage();

    expect(screen.getByRole("heading", { name: "Schedules" })).toBeInTheDocument();
    expect(screen.getAllByText("Maya Chen").length).toBeGreaterThan(0);
    expect(screen.getByText("1 active")).toBeInTheDocument();

    const calendarDay = screen
      .getAllByRole("button")
      .find((button) => button.textContent?.includes("scheduled"));
    expect(calendarDay).toBeDefined();
    fireEvent.click(calendarDay!);

    expect(
      await screen.findByRole("heading", { name: "Create Scheduled Task" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("weekdays at 9am"), {
      target: { value: "weekdays at 8:30am" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Derive/ }));
    await waitFor(() => expect(draftCadence).toHaveBeenCalledWith("weekdays at 8:30am"));

    fireEvent.click(screen.getByRole("button", { name: "Create Schedule" }));

    await waitFor(() =>
      expect(createSchedule).toHaveBeenCalledWith(
        expect.objectContaining({
          agent_id: "ag_maya",
          task_id: "task_wf_weekly-business-review",
          schedule_kind: "cron",
          schedule_expr: "30 8 * * 1-5",
        }),
      ),
    );
  });
});
