import { CheckCircle2Icon, CheckIcon, ListChecksIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { Field } from "@/components/shared/Field";
import { InfoCell } from "@/components/shared/InfoCell";
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
import type { GoalRecord, GoalStatus } from "@/lib/goalsApi";
import {
  STATUS_OPTIONS,
  activationLabel,
  displayTarget,
  formattedTime,
  readinessLabel,
  statusLabel,
} from "./goals-utils";
import { MilestoneRail } from "./MilestoneRail";

export function GoalDetail({
  goal,
  busy,
  newDependency,
  setNewDependency,
  onActivate,
  onStatus,
  onAddDependency,
  onDependencyStatus,
  onDelete,
}: {
  goal: GoalRecord;
  busy: boolean;
  newDependency: string;
  setNewDependency: (value: string) => void;
  onActivate: () => void;
  onStatus: (status: GoalStatus) => void;
  onAddDependency: () => void;
  onDependencyStatus: (dependencyId: string, status: "satisfied" | "waived") => void;
  onDelete: () => void;
}) {
  return (
    <div className="rounded-md border border-border">
      <div className="border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{goal.title}</h2>
            <p className="truncate text-xs text-muted-foreground">{displayTarget(goal)}</p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <Badge variant={goal.activation_state === "ready" ? "secondary" : "outline"}>
              {activationLabel(goal.activation_state)}
            </Badge>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Delete goal"
              disabled={busy}
              onClick={onDelete}
            >
              <Trash2Icon />
            </Button>
          </div>
        </div>
      </div>
      <div className="space-y-3 p-3">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <InfoCell label="Status" value={statusLabel(goal.status)} />
          <InfoCell label="Readiness" value={readinessLabel(goal.readiness_kind)} />
          <InfoCell label="Priority" value={`P${goal.priority}`} />
          <InfoCell label="Updated" value={formattedTime(goal.updated_at)} />
        </div>

        <MilestoneRail goal={goal} />

        <Field label="Lifecycle">
          <Select
            value={goal.status}
            onValueChange={(value) => onStatus(value as GoalStatus)}
            disabled={busy || goal.status === "done"}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((status) => (
                <SelectItem key={status} value={status}>
                  {statusLabel(status)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        {goal.activation_state !== "ready" && (
          <Button variant="outline" className="w-full" disabled={busy} onClick={onActivate}>
            <CheckCircle2Icon /> Activate now
          </Button>
        )}

        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <ListChecksIcon className="size-3.5" />
            Dependencies
          </div>
          {goal.dependencies.length === 0 && (
            <div className="rounded-md border border-dashed border-border px-3 py-3 text-sm text-muted-foreground">
              No dependencies.
            </div>
          )}
          {goal.dependencies.map((dependency) => (
            <div key={dependency.id} className="rounded-md border border-border p-2">
              <div className="mb-2 flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="break-words text-sm">{dependency.label}</p>
                  <p className="text-xs text-muted-foreground">{dependency.kind}</p>
                </div>
                <Badge variant={dependency.status === "pending" ? "outline" : "secondary"}>
                  {dependency.status}
                </Badge>
              </div>
              {dependency.status === "pending" && (
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => onDependencyStatus(dependency.id, "satisfied")}
                  >
                    <CheckIcon /> Satisfy
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => onDependencyStatus(dependency.id, "waived")}
                  >
                    Waive
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="flex gap-2">
          <Input
            value={newDependency}
            onChange={(event) => setNewDependency(event.target.value)}
            aria-label="New dependency"
          />
          <Button
            variant="outline"
            size="icon"
            aria-label="Add dependency"
            disabled={busy || newDependency.trim().length === 0}
            onClick={onAddDependency}
          >
            <PlusIcon />
          </Button>
        </div>
      </div>
    </div>
  );
}