import { CheckIcon, CircleDashedIcon, SparklesIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import type { GoalPlannerSource } from "@/lib/goalsApi";
import { iconForScope } from "./goals-icons";
import type { ScopeOption } from "./goals-utils";

export function PlannerPanel({
  scope,
  sources,
  busy,
  error,
  onStart,
}: {
  scope: ScopeOption;
  sources: GoalPlannerSource[];
  busy: boolean;
  error: string | null;
  onStart: (sourceIds: string[]) => Promise<void>;
}) {
  const availableSourceIds = useMemo(
    () => sources.filter((source) => source.available).map((source) => source.id),
    [sources],
  );
  const sourceSignature = availableSourceIds.join("|");
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([]);

  useEffect(() => {
    setSelectedSourceIds(availableSourceIds);
  }, [sourceSignature]); // eslint-disable-line react-hooks/exhaustive-deps

  function toggleSource(source: GoalPlannerSource) {
    if (!source.available) return;
    setSelectedSourceIds((current) =>
      current.includes(source.id)
        ? current.filter((sourceId) => sourceId !== source.id)
        : [...current, source.id],
    );
  }

  return (
    <div className="rounded-md border border-border">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <SparklesIcon className="size-4 text-muted-foreground" />
        <h2 className="text-sm font-semibold">Planning assistant</h2>
      </div>
      <div className="space-y-3 p-3">
        <div className="rounded-md border border-border bg-muted/30 px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            {iconForScope(scope.kind, "size-3.5")}
            <div className="min-w-0">
              <div className="truncate text-sm font-medium">{scope.label}</div>
              <div className="truncate text-xs text-muted-foreground">{scope.subtitle}</div>
            </div>
          </div>
        </div>

        <div className="space-y-2">
          <div className="text-xs font-medium text-muted-foreground">Sources</div>
          <div className="flex flex-wrap gap-2">
            {sources.map((source) => {
              const selected = selectedSourceIds.includes(source.id);
              return (
                <Button
                  key={source.id}
                  type="button"
                  variant={selected ? "secondary" : "outline"}
                  size="sm"
                  disabled={!source.available || busy}
                  onClick={() => toggleSource(source)}
                  title={source.reason ?? source.label}
                >
                  {selected ? <CheckIcon /> : <CircleDashedIcon />}
                  {source.label}
                </Button>
              );
            })}
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
        <Button
          className="w-full"
          disabled={busy}
          onClick={() => void onStart(selectedSourceIds)}
        >
          <SparklesIcon /> Start interview
        </Button>
      </div>
    </div>
  );
}