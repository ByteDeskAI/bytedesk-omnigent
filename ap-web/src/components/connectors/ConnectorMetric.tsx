export function ConnectorMetric({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="rounded-md border border-border/70 bg-muted/20 px-2 py-1 text-xs text-muted-foreground">
      <span className="font-medium text-foreground">{value}</span> {label}
    </span>
  );
}