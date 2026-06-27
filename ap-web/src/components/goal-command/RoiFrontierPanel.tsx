import { useMemo } from "react";
import { ClockIcon, FlagIcon, TrendingUpIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useGoalFrontier } from "@/hooks/useGoals";
import type { GoalFrontierRow, GoalTargetKind } from "@/lib/goalsApi";
import {
  CockpitCard,
  CockpitEmpty,
  CockpitError,
  CockpitLoading,
  formatCents,
  formatConfidence,
} from "./cockpit";

function riskVariant(tier: string): "secondary" | "outline" | "destructive" {
  if (tier === "high") return "destructive";
  if (tier === "low") return "secondary";
  return "outline";
}

function FrontierRow({ row }: { row: GoalFrontierRow }) {
  return (
    <li className="rounded-md border border-border bg-background p-3">
      <div className="mb-2 flex min-w-0 items-start justify-between gap-2">
        <p className="min-w-0 flex-1 truncate text-sm font-semibold">{row.title}</p>
        <Badge variant="secondary" className="tabular-nums">
          <TrendingUpIcon /> {row.roi.toFixed(2)}
        </Badge>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <Badge variant="outline">
          <FlagIcon /> P{row.priority}
        </Badge>
        <Badge variant="outline" className="tabular-nums">
          EV {formatCents(row.expected_value_cents)}
        </Badge>
        <Badge variant={riskVariant(row.risk_tier)}>{row.risk_tier} risk</Badge>
        <Badge variant="outline" className="tabular-nums">
          {formatConfidence(row.confidence)} conf
        </Badge>
      </div>
      {!row.actionable && row.waiting_reasons.length > 0 && (
        <ul className="mt-2 space-y-1">
          {row.waiting_reasons.map((reason) => (
            <li
              key={reason}
              className="flex items-center gap-1.5 text-xs text-muted-foreground"
            >
              <ClockIcon className="size-3 shrink-0" />
              <span className="min-w-0 truncate">{reason}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function RoiFrontierPanel({
  scope,
}: {
  scope?: { target_kind?: GoalTargetKind; target_id?: string };
}) {
  const frontier = useGoalFrontier(scope ?? {});
  const rows = useMemo(() => frontier.data ?? [], [frontier.data]);
  const actionable = useMemo(() => rows.filter((row) => row.actionable), [rows]);
  const waiting = useMemo(() => rows.filter((row) => !row.actionable), [rows]);

  return (
    <CockpitCard
      title="ROI frontier"
      icon={<TrendingUpIcon className="size-4" />}
      action={
        rows.length > 0 ? (
          <span className="shrink-0 text-xs text-muted-foreground">
            {actionable.length} actionable · {waiting.length} waiting
          </span>
        ) : undefined
      }
    >
      {frontier.isLoading ? (
        <CockpitLoading>Loading the frontier…</CockpitLoading>
      ) : frontier.isError ? (
        <CockpitError>Unable to load the ROI frontier.</CockpitError>
      ) : rows.length === 0 ? (
        <CockpitEmpty>No ranked goals yet. Set one in the commander chat.</CockpitEmpty>
      ) : (
        <div className="space-y-3">
          <ul className="space-y-2">
            {actionable.map((row) => (
              <FrontierRow key={row.goal_id} row={row} />
            ))}
          </ul>
          {waiting.length > 0 && (
            <div className="space-y-2">
              <p className="text-xs font-medium text-muted-foreground">Waiting</p>
              <ul className="space-y-2">
                {waiting.map((row) => (
                  <FrontierRow key={row.goal_id} row={row} />
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </CockpitCard>
  );
}
