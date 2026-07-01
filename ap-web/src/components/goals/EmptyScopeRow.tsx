export function EmptyScopeRow({ label }: { label: string }) {
  return (
    <div className="mb-1 rounded-md border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
      {label}
    </div>
  );
}