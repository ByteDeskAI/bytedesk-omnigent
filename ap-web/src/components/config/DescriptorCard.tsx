import { LockIcon } from "lucide-react";
import type { ConfigDescriptor } from "@/hooks/useConfigDescriptors";
import { ConfigValueCell } from "./ConfigValueCell";
import { TIER_LABELS } from "./config-utils";

export function DescriptorCard({ descriptor }: { descriptor: ConfigDescriptor }) {
  const tierLabel = TIER_LABELS[descriptor.tier] ?? `Tier ${descriptor.tier}`;
  return (
    <div className="rounded-lg border border-border bg-background p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-medium">{descriptor.key}</span>
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              Tier {descriptor.tier} · {tierLabel}
            </span>
            {descriptor.sensitivity === "secret" && (
              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                secret
              </span>
            )}
            {descriptor.writable ? (
              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                editable
              </span>
            ) : (
              <span className="flex items-center gap-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                <LockIcon className="size-2.5" /> read-only
              </span>
            )}
          </div>
          {descriptor.what && (
            <p className="mt-1 text-xs text-muted-foreground">{descriptor.what}</p>
          )}
        </div>
      </div>

      <div className="ml-0 mt-2 rounded-md border border-border/60 bg-muted/40 px-3 py-2">
        <div className="flex items-baseline gap-1.5 text-xs">
          <span className="font-medium text-foreground/80">value</span>
          <ConfigValueCell descriptor={descriptor} />
        </div>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground/70">
          <span>storage: {descriptor.storage_source}</span>
          <span>effect: {descriptor.effect_timing}</span>
        </div>
        {!descriptor.writable && descriptor.read_only_reason && (
          <p className="mt-1 text-[11px] text-muted-foreground">{descriptor.read_only_reason}</p>
        )}
      </div>
    </div>
  );
}