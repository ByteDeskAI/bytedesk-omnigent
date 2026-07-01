import { CalendarClockIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  AgentRow,
  CalendarSurface,
  CreateScheduleSurface,
  type CalendarView,
  type SurfaceMode,
  type WorkflowMode,
} from "@/components/schedules";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { ScheduleOccurrence, TaskTemplate } from "@/lib/schedulesApi";
import { Link } from "@/lib/routing";

export interface SchedulesPageShellProps {
  mode: SurfaceMode;
  setMode: (mode: SurfaceMode) => void;
  selectedAgent: AvailableAgent | undefined;
  agentRows: AvailableAgent[];
  selectedAgentId: string | undefined;
  setSelectedAgentId: (id: string) => void;
  view: CalendarView;
  setView: (view: CalendarView) => void;
  calendarDate: Date;
  setCalendarDate: (date: Date) => void;
  occurrences: ScheduleOccurrence[];
  schedulesCount: number;
  loading: boolean;
  openCreate: (slot: Date) => void;
  selectedSlot: Date;
  workflowMode: WorkflowMode;
  setWorkflowMode: (mode: WorkflowMode) => void;
  taskRows: TaskTemplate[];
  selectedTaskId: string | undefined;
  setSelectedTaskId: (id: string | undefined) => void;
  scheduleTitle: string;
  setScheduleTitle: (title: string) => void;
  assistantPrompt: string;
  setAssistantPrompt: (prompt: string) => void;
  naturalCadence: string;
  setNaturalCadence: (cadence: string) => void;
  scheduleKind: "interval" | "cron" | "once";
  setScheduleKind: (kind: "interval" | "cron" | "once") => void;
  scheduleExpr: string;
  setScheduleExpr: (expr: string) => void;
  newWorkflowTitle: string;
  setNewWorkflowTitle: (title: string) => void;
  newWorkflowPrompt: string;
  setNewWorkflowPrompt: (prompt: string) => void;
  error: string | null;
  busy: boolean;
  canSave: boolean;
  applyCadenceDraft: () => void;
  saveSchedule: () => void;
}

export function SchedulesPageShell({
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
  occurrences,
  schedulesCount,
  loading,
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
  applyCadenceDraft,
  saveSchedule,
}: SchedulesPageShellProps) {
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
              occurrences={occurrences}
              schedulesCount={schedulesCount}
              loading={loading}
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