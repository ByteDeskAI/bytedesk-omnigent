import type { PolicyDialogPickerProps } from "./types";

export function PolicyHandlerPicker({
  registry,
  appliedHandlers,
  filter,
  onFilterChange,
  onSelect,
  emptyAllMessage,
}: PolicyDialogPickerProps) {
  const available = registry.filter((r) => !appliedHandlers.has(r.handler));
  const lowerFilter = filter.toLowerCase();
  const filtered = lowerFilter
    ? available.filter(
        (r) =>
          r.name.toLowerCase().includes(lowerFilter) ||
          r.description?.toLowerCase().includes(lowerFilter),
      )
    : available;

  return (
    <>
      <input
        type="text"
        value={filter}
        onChange={(e) => onFilterChange(e.target.value)}
        placeholder="Filter policies..."
        className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
        // eslint-disable-next-line jsx-a11y/no-autofocus
        autoFocus
      />
      <div className="flex max-h-52 flex-col divide-y divide-border overflow-y-auto rounded border border-border">
        {filtered.map((r) => (
          <button
            key={r.handler}
            type="button"
            onClick={() => onSelect(r.handler)}
            className="flex flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-muted"
          >
            <span className="text-sm">{r.name}</span>
            {r.description && (
              <span className="line-clamp-2 text-[11px] text-muted-foreground">{r.description}</span>
            )}
          </button>
        ))}
        {filtered.length === 0 && (
          <p className="py-2 text-center text-xs text-muted-foreground">
            {available.length === 0 ? emptyAllMessage : "No policies match your filter."}
          </p>
        )}
      </div>
    </>
  );
}