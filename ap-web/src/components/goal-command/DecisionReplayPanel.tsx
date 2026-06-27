import { useMemo } from "react";
import { GitBranchIcon, ScrollTextIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useGoalDecisions } from "@/hooks/useGoals";
import {
  CockpitCard,
  CockpitEmpty,
  CockpitError,
  CockpitLoading,
  formatCents,
  formatTime,
} from "./cockpit";

export function DecisionReplayPanel() {
  const decisions = useGoalDecisions();
  const recent = useMemo(
    () => [...(decisions.data ?? [])].sort((a, b) => b.created_at - a.created_at).slice(0, 12),
    [decisions.data],
  );

  return (
    <CockpitCard title="Decision replay" icon={<ScrollTextIcon className="size-4" />}>
      {decisions.isLoading ? (
        <CockpitLoading>Loading decisions…</CockpitLoading>
      ) : decisions.isError ? (
        <CockpitError>Unable to load the decision log.</CockpitError>
      ) : recent.length === 0 ? (
        <CockpitEmpty>No fund/spawn decisions yet.</CockpitEmpty>
      ) : (
        <ul className="space-y-2">
          {recent.map((decision) => (
            <li key={decision.id} className="rounded-md border border-border p-2">
              <div className="mb-1 flex items-start justify-between gap-2">
                <p className="min-w-0 flex-1 break-words text-sm">{decision.reason}</p>
                <Badge variant="outline" className="shrink-0 tabular-nums">
                  ROI {decision.roi_at_decision.toFixed(2)}
                </Badge>
              </div>
              <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                <span className="tabular-nums">{formatTime(decision.created_at)}</span>
                {decision.budget_before !== null && decision.budget_after !== null && (
                  <span className="tabular-nums">
                    {formatCents(decision.budget_before)} → {formatCents(decision.budget_after)}
                  </span>
                )}
                {decision.spawned_session_id && (
                  <Badge variant="secondary">
                    <GitBranchIcon /> spawned
                  </Badge>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </CockpitCard>
  );
}
