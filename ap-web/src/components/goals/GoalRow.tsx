import { FlagIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { GoalRecord } from "@/lib/goalsApi";
import { cn } from "@/lib/utils";
import { activationIcon, iconForScope } from "./goals-icons";
import {
  activationLabel,
  displayTarget,
  formattedTime,
  pendingDependencyCount,
  readinessLabel,
  statusLabel,
} from "./goals-utils";

export function GoalRow({
  goal,
  selected,
  onSelect,
}: {
  goal: GoalRecord;
  selected: boolean;
  onSelect: () => void;
}) {
  const pending = pendingDependencyCount(goal);
  return (
    <button
      type="button"
      className={cn(
        "mb-2 grid min-h-24 w-full cursor-pointer grid-cols-[minmax(0,1fr)_auto] gap-3 rounded-md border p-3 text-left transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50",
        selected ? "border-border bg-muted" : "border-border/70 bg-background hover:bg-muted/50",
      )}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <span className="min-w-0">
        <span className="mb-2 flex min-w-0 items-center gap-2">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-muted/40">
            {iconForScope(goal.target_kind)}
          </span>
          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold">{goal.title}</span>
            <span className="block truncate text-xs text-muted-foreground">
              {displayTarget(goal)}
            </span>
          </span>
        </span>
        <span className="flex flex-wrap gap-1.5">
          <Badge variant="outline">
            <FlagIcon /> P{goal.priority}
          </Badge>
          <Badge variant={goal.activation_state === "ready" ? "secondary" : "outline"}>
            {activationIcon(goal)}
            {activationLabel(goal.activation_state)}
          </Badge>
          <Badge variant={goal.status === "blocked" ? "destructive" : "outline"}>
            {statusLabel(goal.status)}
          </Badge>
          {pending > 0 && <Badge variant="outline">{pending} pending</Badge>}
        </span>
      </span>
      <span className="text-right text-xs text-muted-foreground">
        <span className="block">{readinessLabel(goal.readiness_kind)}</span>
        <span className="block tabular-nums">{formattedTime(goal.updated_at)}</span>
      </span>
    </button>
  );
}