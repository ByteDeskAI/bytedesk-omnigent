import { useMemo } from "react";
import { CoinsIcon } from "lucide-react";
import { useGoalOutcomes } from "@/hooks/useGoals";
import {
  CockpitCard,
  CockpitEmpty,
  CockpitError,
  CockpitLoading,
  formatCents,
  formatTime,
} from "./cockpit";

export function TreasuryPanel() {
  const outcomes = useGoalOutcomes();
  const rows = useMemo(() => outcomes.data ?? [], [outcomes.data]);
  const realized = useMemo(
    () => rows.reduce((sum, outcome) => sum + outcome.realized_value_cents, 0),
    [rows],
  );
  const recent = useMemo(
    () => [...rows].sort((a, b) => b.booked_at - a.booked_at).slice(0, 8),
    [rows],
  );

  return (
    <CockpitCard title="Treasury" icon={<CoinsIcon className="size-4" />}>
      {outcomes.isLoading ? (
        <CockpitLoading>Loading realized value…</CockpitLoading>
      ) : outcomes.isError ? (
        <CockpitError>Unable to load the treasury ledger.</CockpitError>
      ) : (
        <div className="space-y-3">
          <div className="rounded-md border border-border bg-muted/30 px-3 py-2">
            <div className="text-xs text-muted-foreground">Realized value</div>
            <div className="text-lg font-semibold tabular-nums">{formatCents(realized)}</div>
          </div>
          {recent.length === 0 ? (
            <CockpitEmpty>No outcomes booked yet.</CockpitEmpty>
          ) : (
            <ul className="space-y-2">
              {recent.map((outcome) => (
                <li
                  key={outcome.id}
                  className="flex items-center justify-between gap-2 rounded-md border border-border px-2 py-1.5"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm">{outcome.source}</p>
                    <p className="truncate text-xs text-muted-foreground">
                      {formatTime(outcome.booked_at)}
                    </p>
                  </div>
                  <span className="shrink-0 text-sm font-medium tabular-nums">
                    {formatCents(outcome.realized_value_cents)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </CockpitCard>
  );
}
