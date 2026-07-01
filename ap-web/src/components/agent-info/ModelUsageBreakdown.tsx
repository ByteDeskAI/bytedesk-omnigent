import type { ModelUsage } from "@/lib/types";
import {
  formatSessionCostUsd,
  formatTokenCount,
  MODEL_TOKEN_ROWS,
} from "./agent-info-utils";
import { SectionLabel } from "./SectionLabel";

export function ModelUsageBreakdown({ usageByModel }: { usageByModel: Record<string, ModelUsage> }) {
  const models = Object.entries(usageByModel).sort(
    ([, a], [, b]) => (b.totalTokens ?? 0) - (a.totalTokens ?? 0),
  );
  return (
    <details data-testid="agent-info-usage-by-model">
      <summary className="cursor-pointer select-none list-none">
        <SectionLabel>
          <span className="inline-flex items-center gap-1">
            Token usage
            <span className="text-[9px]">▶</span>
          </span>
        </SectionLabel>
      </summary>
      <div className="mt-1.5 flex flex-col gap-2">
        {models.map(([model, usage]) => {
          const rows = MODEL_TOKEN_ROWS.flatMap(({ key, label }) => {
            const value = usage[key];
            return value != null ? [{ label, value }] : [];
          });
          return (
            <div
              key={model}
              className="flex flex-col gap-0.5"
              data-testid={`agent-info-model-${model}`}
            >
              <span className="truncate font-mono text-[11px] text-muted-foreground" title={model}>
                {model}
              </span>
              {rows.map((row) => (
                <div
                  key={row.label}
                  className="flex items-baseline justify-between gap-3 pl-2 text-xs"
                >
                  <span className="text-muted-foreground/70">{row.label}</span>
                  <span className="tabular-nums text-muted-foreground">
                    {formatTokenCount(row.value)}
                  </span>
                </div>
              ))}
              {usage.totalCostUsd != null && (
                <div className="flex items-baseline justify-between gap-3 pl-2 text-xs">
                  <span className="text-muted-foreground/70">Cost</span>
                  <span className="tabular-nums text-muted-foreground">
                    {formatSessionCostUsd(usage.totalCostUsd)}
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}