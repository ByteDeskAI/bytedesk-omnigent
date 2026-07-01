import { GoalRow } from "@/components/goals";
import type { GoalView } from "@/components/goals/goals-utils";
import { ViewButton } from "@/components/shared";
import type { GoalRecord } from "@/lib/goalsApi";

export interface GoalsListMainProps {
  view: GoalView;
  setView: (view: GoalView) => void;
  goalsFetching: boolean;
  filteredGoals: GoalRecord[];
  selectedGoalId: string | null;
  setSelectedGoalId: (id: string | null) => void;
}

export function GoalsListMain({
  view,
  setView,
  goalsFetching,
  filteredGoals,
  selectedGoalId,
  setSelectedGoalId,
}: GoalsListMainProps) {
  return (
    <main className="min-h-0 overflow-hidden border-b border-border lg:border-r lg:border-b-0">
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
          <div className="flex items-center gap-1">
            <ViewButton active={view === "active"} onClick={() => setView("active")}>
              Active
            </ViewButton>
            <ViewButton active={view === "waiting"} onClick={() => setView("waiting")}>
              Waiting
            </ViewButton>
            <ViewButton active={view === "done"} onClick={() => setView("done")}>
              Done
            </ViewButton>
          </div>
          <div className="text-xs text-muted-foreground">
            {goalsFetching ? "Refreshing" : `${filteredGoals.length} shown`}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-3">
          {filteredGoals.map((goal) => (
            <GoalRow
              key={goal.id}
              goal={goal}
              selected={goal.id === selectedGoalId}
              onSelect={() => setSelectedGoalId(goal.id)}
            />
          ))}
          {filteredGoals.length === 0 && (
            <div className="flex min-h-52 items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
              No goals match this view.
            </div>
          )}
        </div>
      </div>
    </main>
  );
}