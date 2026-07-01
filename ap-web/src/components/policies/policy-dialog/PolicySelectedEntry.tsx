import type { PolicySelectedEntryProps } from "./types";

export function PolicySelectedEntry({ entry, onClear }: PolicySelectedEntryProps) {
  return (
    <div className="flex flex-col gap-1 rounded border border-border bg-muted/50 px-2.5 py-2">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">{entry.name}</span>
        <button
          type="button"
          onClick={onClear}
          className="text-[11px] text-muted-foreground hover:text-foreground"
        >
          Change
        </button>
      </div>
      {entry.description && <p className="text-xs text-muted-foreground">{entry.description}</p>}
    </div>
  );
}