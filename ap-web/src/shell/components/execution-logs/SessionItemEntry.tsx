import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import { useState } from "react";
import type { RawSessionItem } from "@/hooks/useSessionItems";

export function SessionItemEntry({ item, index }: { item: RawSessionItem; index: number }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const collapsed = JSON.stringify(item);
  const expanded = JSON.stringify(item, null, 2);
  return (
    <div className="rounded-md border border-border bg-muted/40">
      <button
        type="button"
        aria-expanded={isExpanded}
        data-testid="execution-log-entry"
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left hover:bg-muted/60"
        onClick={() => setIsExpanded((v) => !v)}
      >
        {isExpanded ? (
          <ChevronDownIcon className="size-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRightIcon className="size-3 shrink-0 text-muted-foreground" />
        )}
        <span className="shrink-0 text-muted-foreground">#{index}</span>
        {!isExpanded && <span className="truncate text-foreground">{collapsed}</span>}
      </button>
      {isExpanded && (
        <pre className="whitespace-pre-wrap break-words border-t border-border px-2 py-1.5 text-foreground">
          {expanded}
        </pre>
      )}
    </div>
  );
}