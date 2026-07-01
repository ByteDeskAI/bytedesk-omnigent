export function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="mc-surface flex items-center gap-1.5 px-2 py-1">
      <span className="mc-value text-xs">{value}</span>
      <span className="mc-label text-2xs text-muted-foreground">{label}</span>
    </span>
  );
}