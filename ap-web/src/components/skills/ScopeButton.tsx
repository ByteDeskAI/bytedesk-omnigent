import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function ScopeButton({
  icon,
  label,
  subtitle,
  count,
  selected,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  subtitle: string;
  count: number | undefined;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "mb-1 flex min-h-12 w-full cursor-pointer items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50",
        selected
          ? "border-border bg-muted text-foreground"
          : "border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground",
      )}
      aria-pressed={selected}
    >
      <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-background">
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{label}</span>
        <span className="block truncate text-xs text-muted-foreground">{subtitle}</span>
      </span>
      {count !== undefined && <Badge variant="secondary">{count}</Badge>}
    </button>
  );
}