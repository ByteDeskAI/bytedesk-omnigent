import { FlagIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { GoalRecord } from "@/lib/goalsApi";
import { milestoneIcon } from "./goals-icons";
import { goalMilestones, milestoneLabel } from "./goals-utils";

export function MilestoneRail({ goal }: { goal: GoalRecord }) {
  const milestones = goalMilestones(goal);
  if (milestones.length === 0) return null;
  const doneCount = milestones.filter((milestone) => milestone.status === "done").length;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs font-medium text-muted-foreground">
        <span className="flex items-center gap-2">
          <FlagIcon className="size-3.5" /> Milestones
        </span>
        <span>
          {doneCount}/{milestones.length} done
        </span>
      </div>
      <ol className="space-y-1.5">
        {milestones.map((milestone, index) => {
          const steps = milestone.steps ?? [];
          const stepsDone = milestone.stepsDone ?? [];
          return (
            <li
              key={milestone.taskKey ?? index}
              className="flex items-center justify-between gap-2 rounded-md border border-border px-2 py-1.5"
            >
              <div className="min-w-0">
                <p className="truncate text-sm">
                  {milestone.title ?? milestone.taskKey ?? `Milestone ${index + 1}`}
                </p>
                <p className="truncate text-xs text-muted-foreground">
                  {milestone.taskKey ?? ""}
                  {steps.length > 0 ? ` · ${stepsDone.length}/${steps.length} steps` : ""}
                </p>
              </div>
              <Badge variant={milestone.status === "done" ? "secondary" : "outline"}>
                {milestoneIcon(milestone.status)}
                {milestoneLabel(milestone.status)}
              </Badge>
            </li>
          );
        })}
      </ol>
    </div>
  );
}