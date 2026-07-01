import { Clock3Icon, PlusIcon, SparklesIcon } from "lucide-react";
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
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { TaskTemplate } from "@/lib/schedulesApi";
import { monthFormatter, timeFormatter, type WorkflowMode } from "./schedules-utils";

export function CreateScheduleSurface({
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