import { useEffect, useMemo, useState } from "react";
import {
  CalendarClockIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  Clock3Icon,
  PlusIcon,
  SparklesIcon,
  XIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { tierForAgent, TIER_LABELS } from "@/lib/agentTiers";
import {
  useCreateSchedule,
  useCreateTaskTemplate,
  useDraftCadence,
  useScheduleOccurrences,
  useSchedules,
  useTaskTemplates,
} from "@/hooks/useSchedules";
import { Link } from "@/lib/routing";
import type { ScheduleOccurrence, TaskTemplate } from "@/lib/schedulesApi";

type CalendarView = "week" | "day";
type SurfaceMode = "calendar" | "create";
type WorkflowMode = "existing" | "new";

const dayFormatter = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
});
const timeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
});
const monthFormatter = new Intl.DateTimeFormat(undefined, {
  month: "long",
  day: "numeric",
  year: "numeric",
});

function startOfDay(date: Date): Date {
  const next = new Date(date);
  next.setHours(0, 0, 0, 0);
  return next;
}

function startOfWeek(date: Date): Date {
  const next = startOfDay(date);
  const day = next.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  next.setDate(next.getDate() + diff);
  return next;
}

function addDays(date: Date, amount: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + amount);
  return next;
}

function addHours(date: Date, hour: number): Date {
  const next = startOfDay(date);
  next.setHours(hour, 0, 0, 0);
  return next;
}

function dayKey(epochSeconds: number): string {
  return startOfDay(new Date(epochSeconds * 1000)).toISOString();
}

function occurrenceHour(epochSeconds: number): number {
  return new Date(epochSeconds * 1000).getHours();
}

function taskPrompt(task: TaskTemplate | undefined): string {
  const prompt = task?.payload?.prompt;
  return typeof prompt === "string" ? prompt : "";
}

export function SchedulesPage() {
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

  function openCreate(slot: Date) {
    setSelectedSlot(slot);
    setMode("create");
    setError(null);
    const defaultTitle = selectedTask?.title ?? "Scheduled task";
    setScheduleTitle(defaultTitle);
    setAssistantPrompt(taskPrompt(selectedTask));
    if (!newWorkflowTitle) setNewWorkflowTitle(defaultTitle);
  }

  async function applyCadenceDraft() {
    setError(null);
    try {
      const draft = await draftCadence.mutateAsync(naturalCadence);
      setScheduleKind(draft.schedule_kind);
      setScheduleExpr(draft.schedule_expr);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to derive cadence.");
    }
  }

  async function saveSchedule() {
    if (!selectedAgentId) return;
    setError(null);
    try {
      let taskId = selectedTaskId;
      if (workflowMode === "new") {
        const task = await createTask.mutateAsync({
          title: newWorkflowTitle.trim(),
          prompt: newWorkflowPrompt.trim() || assistantPrompt.trim(),
          owner_agent_id: selectedAgentId,
          payload: {
            planned_with: "schedules",
            agent_id: selectedAgentId,
          },
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
  }

  const busy =
    createSchedule.isPending || createTask.isPending || draftCadence.isPending || tasks.isLoading;
  const canSave =
    Boolean(selectedAgentId) &&
    scheduleTitle.trim().length > 0 &&
    scheduleExpr.trim().length > 0 &&
    (workflowMode === "existing" ? Boolean(selectedTaskId) : newWorkflowTitle.trim().length > 0);

  return (
    <div className="fixed inset-3 z-50 flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-background shadow-2xl">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted">
            <CalendarClockIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold">Schedules</h1>
            <p className="truncate text-xs text-muted-foreground">
              {selectedAgent ? selectedAgent.display_name : "Select an agent"}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {mode === "create" && (
            <Button variant="outline" size="sm" onClick={() => setMode("calendar")}>
              Calendar
            </Button>
          )}
          <Button variant="ghost" size="icon" asChild aria-label="Close schedules">
            <Link to="/">
              <XIcon />
            </Link>
          </Button>
        </div>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[18rem_minmax(0,1fr)]">
        <aside className="min-h-0 border-b border-border md:border-r md:border-b-0">
          <div className="flex h-full min-h-0 flex-col">
            <div className="shrink-0 border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">
              Agents
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-2">
              {agentRows.map((agent) => (
                <AgentRow
                  key={agent.id}
                  agent={agent}
                  selected={agent.id === selectedAgentId}
                  onSelect={() => {
                    setSelectedAgentId(agent.id);
                    setMode("calendar");
                  }}
                />
              ))}
              {agentRows.length === 0 && (
                <p className="px-2 py-3 text-sm text-muted-foreground">No agents available.</p>
              )}
            </div>
          </div>
        </aside>

        <main className="min-h-0 overflow-hidden">
          {mode === "calendar" ? (
            <CalendarSurface
              view={view}
              setView={setView}
              calendarDate={calendarDate}
              setCalendarDate={setCalendarDate}
              occurrences={occurrences.data ?? []}
              schedulesCount={schedules.data?.length ?? 0}
              loading={occurrences.isLoading || schedules.isLoading}
              onCreate={openCreate}
            />
          ) : (
            <CreateScheduleSurface
              selectedAgent={selectedAgent}
              selectedSlot={selectedSlot}
              workflowMode={workflowMode}
              setWorkflowMode={setWorkflowMode}
              taskRows={taskRows}
              selectedTaskId={selectedTaskId}
              setSelectedTaskId={setSelectedTaskId}
              scheduleTitle={scheduleTitle}
              setScheduleTitle={setScheduleTitle}
              assistantPrompt={assistantPrompt}
              setAssistantPrompt={setAssistantPrompt}
              naturalCadence={naturalCadence}
              setNaturalCadence={setNaturalCadence}
              scheduleKind={scheduleKind}
              setScheduleKind={setScheduleKind}
              scheduleExpr={scheduleExpr}
              setScheduleExpr={setScheduleExpr}
              newWorkflowTitle={newWorkflowTitle}
              setNewWorkflowTitle={setNewWorkflowTitle}
              newWorkflowPrompt={newWorkflowPrompt}
              setNewWorkflowPrompt={setNewWorkflowPrompt}
              error={error}
              busy={busy}
              canSave={canSave}
              onDraftCadence={applyCadenceDraft}
              onSave={saveSchedule}
            />
          )}
        </main>
      </div>
    </div>
  );
}

function AgentRow({
  agent,
  selected,
  onSelect,
}: {
  agent: AvailableAgent;
  selected: boolean;
  onSelect: () => void;
}) {
  // Employees carry no badge (matching the prior bare-workflow rule); only
  // the System and Workflow tiers get a tier label.
  const tier = tierForAgent(agent);
  return (
    <button
      type="button"
      onClick={onSelect}
      className={[
        "mb-1 flex min-h-14 w-full cursor-pointer items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors",
        selected
          ? "border-primary/50 bg-primary/10 text-foreground"
          : "border-transparent text-muted-foreground hover:border-border hover:bg-muted/60 hover:text-foreground",
      ].join(" ")}
    >
      <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-background text-xs font-medium">
        {agent.display_name.slice(0, 2).toUpperCase()}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{agent.display_name}</span>
        <span className="block truncate text-xs">{agent.title ?? agent.name}</span>
      </span>
      {tier !== "employee" && <Badge variant="secondary">{TIER_LABELS[tier]}</Badge>}
    </button>
  );
}

function CalendarSurface({
  view,
  setView,
  calendarDate,
  setCalendarDate,
  occurrences,
  schedulesCount,
  loading,
  onCreate,
}: {
  view: CalendarView;
  setView: (value: CalendarView) => void;
  calendarDate: Date;
  setCalendarDate: (value: Date) => void;
  occurrences: ScheduleOccurrence[];
  schedulesCount: number;
  loading: boolean;
  onCreate: (slot: Date) => void;
}) {
  const weekStart = startOfWeek(calendarDate);
  const days = Array.from({ length: 7 }, (_, index) => addDays(weekStart, index));
  const occurrencesByDay = useMemo(() => {
    const map = new Map<string, ScheduleOccurrence[]>();
    for (const occurrence of occurrences) {
      const key = dayKey(occurrence.fire_at);
      const next = map.get(key) ?? [];
      next.push(occurrence);
      map.set(key, next);
    }
    return map;
  }, [occurrences]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="icon"
            aria-label="Previous period"
            onClick={() => setCalendarDate(addDays(calendarDate, view === "week" ? -7 : -1))}
          >
            <ChevronLeftIcon />
          </Button>
          <div className="min-w-40 text-sm font-medium">{monthFormatter.format(calendarDate)}</div>
          <Button
            variant="ghost"
            size="icon"
            aria-label="Next period"
            onClick={() => setCalendarDate(addDays(calendarDate, view === "week" ? 7 : 1))}
          >
            <ChevronRightIcon />
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline">{schedulesCount} active</Badge>
          <Button
            variant={view === "week" ? "default" : "outline"}
            size="sm"
            onClick={() => setView("week")}
          >
            Week
          </Button>
          <Button
            variant={view === "day" ? "default" : "outline"}
            size="sm"
            onClick={() => setView("day")}
          >
            Day
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        {view === "week" ? (
          <div className="grid min-h-[28rem] grid-cols-1 gap-2 sm:grid-cols-7">
            {days.map((day) => {
              const items = occurrencesByDay.get(startOfDay(day).toISOString()) ?? [];
              return (
                <button
                  key={day.toISOString()}
                  type="button"
                  onClick={() => onCreate(day)}
                  className="flex min-h-48 cursor-pointer flex-col rounded-md border border-border bg-card p-3 text-left transition-colors hover:border-primary/50 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="text-sm font-medium">{dayFormatter.format(day)}</span>
                  <span className="mt-1 text-xs text-muted-foreground">
                    {items.length} scheduled
                  </span>
                  <span className="mt-3 flex flex-col gap-1">
                    {items.slice(0, 4).map((item) => (
                      <span
                        key={item.id}
                        className="rounded border border-border bg-background px-2 py-1 text-xs text-foreground"
                      >
                        {timeFormatter.format(new Date(item.fire_at * 1000))} {item.title}
                      </span>
                    ))}
                    {items.length > 4 && (
                      <span className="text-xs text-muted-foreground">
                        +{items.length - 4} more
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
        ) : (
          <DayView day={calendarDate} occurrences={occurrences} onCreate={onCreate} />
        )}
        {loading && <p className="mt-3 text-xs text-muted-foreground">Loading schedules…</p>}
      </div>
    </div>
  );
}

function DayView({
  day,
  occurrences,
  onCreate,
}: {
  day: Date;
  occurrences: ScheduleOccurrence[];
  onCreate: (slot: Date) => void;
}) {
  return (
    <div className="grid gap-1">
      {Array.from({ length: 24 }, (_, hour) => {
        const slot = addHours(day, hour);
        const items = occurrences.filter((item) => occurrenceHour(item.fire_at) === hour);
        return (
          <button
            key={hour}
            type="button"
            onClick={() => onCreate(slot)}
            className="grid min-h-14 cursor-pointer grid-cols-[5rem_minmax(0,1fr)] items-stretch rounded-md border border-border bg-card text-left transition-colors hover:border-primary/50 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="flex items-center border-r border-border px-3 text-xs text-muted-foreground">
              {timeFormatter.format(slot)}
            </span>
            <span className="flex min-w-0 flex-wrap items-center gap-1 px-3 py-2">
              {items.length === 0 ? (
                <span className="text-xs text-muted-foreground">Open</span>
              ) : (
                items.map((item) => (
                  <span
                    key={item.id}
                    className="rounded border border-border bg-background px-2 py-1 text-xs"
                  >
                    {item.title}
                  </span>
                ))
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function CreateScheduleSurface({
  selectedAgent,
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
  onDraftCadence,
  onSave,
}: {
  selectedAgent: AvailableAgent | undefined;
  selectedSlot: Date;
  workflowMode: WorkflowMode;
  setWorkflowMode: (value: WorkflowMode) => void;
  taskRows: TaskTemplate[];
  selectedTaskId: string | undefined;
  setSelectedTaskId: (value: string) => void;
  scheduleTitle: string;
  setScheduleTitle: (value: string) => void;
  assistantPrompt: string;
  setAssistantPrompt: (value: string) => void;
  naturalCadence: string;
  setNaturalCadence: (value: string) => void;
  scheduleKind: "interval" | "cron" | "once";
  setScheduleKind: (value: "interval" | "cron" | "once") => void;
  scheduleExpr: string;
  setScheduleExpr: (value: string) => void;
  newWorkflowTitle: string;
  setNewWorkflowTitle: (value: string) => void;
  newWorkflowPrompt: string;
  setNewWorkflowPrompt: (value: string) => void;
  error: string | null;
  busy: boolean;
  canSave: boolean;
  onDraftCadence: () => void;
  onSave: () => void;
}) {
  return (
    <div className="grid h-full min-h-0 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <section className="min-h-0 overflow-auto border-b border-border p-4 lg:border-r lg:border-b-0">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">Create Scheduled Task</h2>
            <p className="text-xs text-muted-foreground">
              {selectedAgent?.display_name ?? "Agent"} · {monthFormatter.format(selectedSlot)} ·{" "}
              {timeFormatter.format(selectedSlot)}
            </p>
          </div>
          <Badge variant="outline">
            <Clock3Icon className="size-3" /> {scheduleKind}
          </Badge>
        </div>

        <div className="grid gap-4">
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">Title</span>
            <Input
              value={scheduleTitle}
              onChange={(event) => setScheduleTitle(event.target.value)}
            />
          </label>

          <div className="grid gap-2">
            <span className="text-sm font-medium">Workflow</span>
            <div className="flex flex-wrap gap-2">
              <Button
                variant={workflowMode === "existing" ? "default" : "outline"}
                size="sm"
                onClick={() => setWorkflowMode("existing")}
              >
                Existing
              </Button>
              <Button
                variant={workflowMode === "new" ? "default" : "outline"}
                size="sm"
                onClick={() => setWorkflowMode("new")}
              >
                New Plan
              </Button>
            </div>
            {workflowMode === "existing" ? (
              <Select value={selectedTaskId} onValueChange={setSelectedTaskId}>
                <SelectTrigger>
                  <SelectValue placeholder="Select workflow" />
                </SelectTrigger>
                <SelectContent>
                  {taskRows.map((task) => (
                    <SelectItem key={task.id} value={task.id}>
                      {task.title}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <div className="grid gap-2">
                <Input
                  value={newWorkflowTitle}
                  onChange={(event) => setNewWorkflowTitle(event.target.value)}
                  placeholder="Workflow title"
                />
                <Textarea
                  value={newWorkflowPrompt}
                  onChange={(event) => setNewWorkflowPrompt(event.target.value)}
                  placeholder="Workflow plan"
                  className="min-h-32"
                />
              </div>
            )}
          </div>

          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">Assistant Draft</span>
            <Textarea
              value={assistantPrompt}
              onChange={(event) => setAssistantPrompt(event.target.value)}
              placeholder="Task instructions"
              className="min-h-36"
            />
          </label>

          <div className="grid gap-2">
            <span className="text-sm font-medium">Cadence</span>
            <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto]">
              <Input
                value={naturalCadence}
                onChange={(event) => setNaturalCadence(event.target.value)}
                placeholder="weekdays at 9am"
              />
              <Button variant="outline" onClick={onDraftCadence} disabled={busy}>
                <SparklesIcon /> Derive
              </Button>
            </div>
            <div className="grid gap-2 md:grid-cols-[12rem_minmax(0,1fr)]">
              <Select
                value={scheduleKind}
                onValueChange={(value) => setScheduleKind(value as typeof scheduleKind)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="cron">Cron</SelectItem>
                  <SelectItem value="interval">Interval</SelectItem>
                  <SelectItem value="once">Once</SelectItem>
                </SelectContent>
              </Select>
              <Input
                value={scheduleExpr}
                onChange={(event) => setScheduleExpr(event.target.value)}
                placeholder={scheduleKind === "interval" ? "3600" : "0 9 * * 1-5"}
              />
            </div>
          </div>

          {error && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {error}
            </div>
          )}
        </div>
      </section>

      <aside className="min-h-0 overflow-auto p-4">
        <div className="grid gap-3">
          <div className="rounded-md border border-border bg-card p-3">
            <div className="text-xs font-medium text-muted-foreground">Selected Agent</div>
            <div className="mt-1 text-sm font-medium">{selectedAgent?.display_name ?? "None"}</div>
            <div className="mt-1 text-xs text-muted-foreground">
              {selectedAgent?.title ?? selectedAgent?.name}
            </div>
          </div>
          <div className="rounded-md border border-border bg-card p-3">
            <div className="text-xs font-medium text-muted-foreground">Cadence Expression</div>
            <div className="mt-1 font-mono text-sm">{scheduleExpr || "unset"}</div>
          </div>
          <Button onClick={onSave} disabled={!canSave || busy}>
            <PlusIcon /> Create Schedule
          </Button>
        </div>
      </aside>
    </div>
  );
}
