import { useEffect, useMemo, useState } from "react";
import {
  addDays,
  startOfDay,
  startOfWeek,
  taskPrompt,
  type CalendarView,
  type SurfaceMode,
  type WorkflowMode,
} from "@/components/schedules";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  useCreateSchedule,
  useCreateTaskTemplate,
  useDraftCadence,
  useScheduleOccurrences,
  useSchedules,
  useTaskTemplates,
} from "@/hooks/useSchedules";
import type { SchedulesPageShellProps } from "./SchedulesPageShell";

export function useSchedulesPageState(): SchedulesPageShellProps {
  const agents = useAvailableAgents();
  const tasks = useTaskTemplates();
  const createTask = useCreateTaskTemplate();
  const createSchedule = useCreateSchedule();
  const draftCadence = useDraftCadence();

  const [selectedAgentId, setSelectedAgentId] = useState<string | undefined>();
  const [calendarDate, setCalendarDate] = useState(() => startOfDay(new Date()));
  const [view, setView] = useState<CalendarView>("week");
  const [mode, setMode] = useState<SurfaceMode>("calendar");
  const [selectedSlot, setSelectedSlot] = useState<Date>(() => new Date());
  const [workflowMode, setWorkflowMode] = useState<WorkflowMode>("existing");
  const [selectedTaskId, setSelectedTaskId] = useState<string | undefined>();
  const [scheduleTitle, setScheduleTitle] = useState("");
  const [assistantPrompt, setAssistantPrompt] = useState("");
  const [naturalCadence, setNaturalCadence] = useState("weekdays at 9am");
  const [scheduleKind, setScheduleKind] = useState<"interval" | "cron" | "once">("cron");
  const [scheduleExpr, setScheduleExpr] = useState("0 9 * * 1-5");
  const [newWorkflowTitle, setNewWorkflowTitle] = useState("");
  const [newWorkflowPrompt, setNewWorkflowPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);

  const agentRows = useMemo(() => agents.data ?? [], [agents.data]);
  const selectedAgent = agentRows.find((agent) => agent.id === selectedAgentId);
  const taskRows = useMemo(() => tasks.data ?? [], [tasks.data]);
  const selectedTask = taskRows.find((task) => task.id === selectedTaskId);

  useEffect(() => {
    if (!selectedAgentId && agentRows.length > 0) setSelectedAgentId(agentRows[0].id);
  }, [agentRows, selectedAgentId]);

  useEffect(() => {
    if (!selectedTaskId && taskRows.length > 0) setSelectedTaskId(taskRows[0].id);
  }, [selectedTaskId, taskRows]);

  const range = useMemo(() => {
    const start = view === "week" ? startOfWeek(calendarDate) : startOfDay(calendarDate);
    const end = view === "week" ? addDays(start, 7) : addDays(start, 1);
    return { start, end };
  }, [calendarDate, view]);

  const schedules = useSchedules(selectedAgentId);
  const occurrences = useScheduleOccurrences(
    selectedAgentId,
    range.start.toISOString(),
    range.end.toISOString(),
  );

  const openCreate = (slot: Date) => {
    setSelectedSlot(slot);
    setMode("create");
    setError(null);
    const defaultTitle = selectedTask?.title ?? "Scheduled task";
    setScheduleTitle(defaultTitle);
    setAssistantPrompt(taskPrompt(selectedTask));
    if (!newWorkflowTitle) setNewWorkflowTitle(defaultTitle);
  };

  const applyCadenceDraft = async () => {
    setError(null);
    try {
      const draft = await draftCadence.mutateAsync(naturalCadence);
      setScheduleKind(draft.schedule_kind);
      setScheduleExpr(draft.schedule_expr);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to derive cadence.");
    }
  };

  const saveSchedule = async () => {
    if (!selectedAgentId) return;
    setError(null);
    try {
      let taskId = selectedTaskId;
      if (workflowMode === "new") {
        const task = await createTask.mutateAsync({
          title: newWorkflowTitle.trim(),
          prompt: newWorkflowPrompt.trim() || assistantPrompt.trim(),
          owner_agent_id: selectedAgentId,
          payload: { planned_with: "schedules", agent_id: selectedAgentId },
        });
        taskId = task.id;
        setSelectedTaskId(task.id);
      }
      await createSchedule.mutateAsync({
        agent_id: selectedAgentId,
        title: scheduleTitle.trim() || selectedTask?.title || newWorkflowTitle || "Scheduled task",
        task_id: taskId,
        prompt: assistantPrompt.trim() || undefined,
        schedule_kind: scheduleKind,
        schedule_expr: scheduleExpr.trim(),
        start_at: selectedSlot.toISOString(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
      setMode("calendar");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create schedule.");
    }
  };

  const busy =
    createSchedule.isPending || createTask.isPending || draftCadence.isPending || tasks.isLoading;
  const canSave =
    Boolean(selectedAgentId) &&
    scheduleTitle.trim().length > 0 &&
    scheduleExpr.trim().length > 0 &&
    (workflowMode === "existing" ? Boolean(selectedTaskId) : newWorkflowTitle.trim().length > 0);

  return {
    mode,
    setMode,
    selectedAgent,
    agentRows,
    selectedAgentId,
    setSelectedAgentId,
    view,
    setView,
    calendarDate,
    setCalendarDate,
    occurrences: occurrences.data ?? [],
    schedulesCount: schedules.data?.length ?? 0,
    loading: occurrences.isLoading || schedules.isLoading,
    openCreate,
    selectedSlot,
    workflowMode,
    setWorkflowMode,
    taskRows,
    selectedTaskId,
    setSelectedTaskId,
    scheduleTitle,
    setScheduleTitle,
    assistantPrompt,
    setAssistantPrompt,
    naturalCadence,
    setNaturalCadence,
    scheduleKind,
    setScheduleKind,
    scheduleExpr,
    setScheduleExpr,
    newWorkflowTitle,
    setNewWorkflowTitle,
    newWorkflowPrompt,
    setNewWorkflowPrompt,
    error,
    busy,
    canSave,
    applyCadenceDraft: () => void applyCadenceDraft(),
    saveSchedule: () => void saveSchedule(),
  };
}