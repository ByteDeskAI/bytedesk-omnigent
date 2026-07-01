import { ChevronDownIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function ScopeAccordion({
  label,
  icon,
  open,
  onToggle,
  count,
  children,
}: {
  label: string;
  icon: ReactNode;
  open: boolean;
  onToggle: () => void;
  count: number;
  children: ReactNode;
}) {
  return (
    <div className="mb-1">
      <button
        type="button"
        onClick={onToggle}
        className="flex min-h-11 w-full cursor-pointer items-center gap-2 rounded-md border border-transparent px-2.5 py-2 text-left text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50"
        aria-expanded={open}
      >
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-background">
          {icon}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{label}</span>
        <Badge variant="outline">{count}</Badge>
        <ChevronDownIcon
          className={cn("size-4 transition-transform", open ? "rotate-180" : "rotate-0")}
        />
      </button>
      {open && <div className="mt-1">{children}</div>}
    </div>
  );
}