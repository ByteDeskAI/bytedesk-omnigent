import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { iconForScope } from "./goals-icons";
import type { ScopeOption } from "./goals-utils";

export function ScopeButton({
  scope,
  selected,
  onSelect,
  nested = false,
}: {
  scope: ScopeOption;
  selected: boolean;
  onSelect: () => void;
  nested?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "mb-1 flex min-h-12 w-full cursor-pointer items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50",
        nested && "pl-4",
        selected
          ? "border-border bg-muted text-foreground"
          : "border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground",
      )}
      aria-pressed={selected}
    >
      <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-background">
        {iconForScope(scope.kind)}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{scope.label}</span>
        <span className="block truncate text-xs text-muted-foreground">{scope.subtitle}</span>
      </span>
      <Badge variant="secondary">{scope.count}</Badge>
    </button>
  );
}