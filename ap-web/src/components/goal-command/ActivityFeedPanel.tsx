import { useMemo } from "react";
import { ActivityIcon, CoinsIcon, GitBranchIcon, SparklesIcon } from "lucide-react";
import { useGoalDecisions, useGoalOutcomes } from "@/hooks/useGoals";
import {
  CockpitCard,
  CockpitEmpty,
  CockpitError,
  CockpitLoading,
  formatCents,
  formatTime,
} from "./cockpit";

type FeedItem =
  | { kind: "spawn"; id: string; at: number; label: string }
  | { kind: "decision"; id: string; at: number; label: string }
  | { kind: "outcome"; id: string; at: number; label: string; valueCents: number };

// The feed is driven by the same goal SSE stream as the rest of the
// cockpit: `useGoalEvents` (mounted once on the page) invalidates the
// decisions/outcomes queries, so this merged view re-renders as the
// engine spawns sessions and books outcomes.
export function ActivityFeedPanel() {
  const decisions = useGoalDecisions();
  const outcomes = useGoalOutcomes();

  const items = useMemo<FeedItem[]>(() => {
    const fromDecisions = (decisions.data ?? []).map<FeedItem>((decision) =>
      decision.spawned_session_id
        ? {
            kind: "spawn",
            id: `spawn:${decision.id}`,
            at: decision.created_at,
            label: decision.reason,
          }
        : {
            kind: "decision",
            id: `decision:${decision.id}`,
            at: decision.created_at,
            label: decision.reason,
          },
    );
    const fromOutcomes = (outcomes.data ?? []).map<FeedItem>((outcome) => ({
      kind: "outcome",
      id: `outcome:${outcome.id}`,
      at: outcome.booked_at,
      label: outcome.source,
      valueCents: outcome.realized_value_cents,
    }));
    return [...fromDecisions, ...fromOutcomes].sort((a, b) => b.at - a.at).slice(0, 20);
  }, [decisions.data, outcomes.data]);

  const isLoading = decisions.isLoading || outcomes.isLoading;
  const isError = decisions.isError || outcomes.isError;

  return (
    <CockpitCard title="Activity" icon={<ActivityIcon className="size-4" />}>
      {isLoading ? (
        <CockpitLoading>Loading activity…</CockpitLoading>
      ) : isError ? (
        <CockpitError>Unable to load live activity.</CockpitError>
      ) : items.length === 0 ? (
        <CockpitEmpty>Nothing happening yet. The engine is idle.</CockpitEmpty>
      ) : (
        <ul className="space-y-2">
          {items.map((item) => (
            <li key={item.id} className="flex items-start gap-2 rounded-md border border-border p-2">
              <span className="mt-0.5 text-muted-foreground">
                {item.kind === "outcome" ? (
                  <CoinsIcon className="size-3.5" />
                ) : item.kind === "spawn" ? (
                  <GitBranchIcon className="size-3.5" />
                ) : (
                  <SparklesIcon className="size-3.5" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <p className="break-words text-sm">{item.label}</p>
                <p className="text-xs text-muted-foreground tabular-nums">{formatTime(item.at)}</p>
              </div>
              {item.kind === "outcome" && (
                <span className="shrink-0 text-sm font-medium tabular-nums">
                  {formatCents(item.valueCents)}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </CockpitCard>
  );
}
