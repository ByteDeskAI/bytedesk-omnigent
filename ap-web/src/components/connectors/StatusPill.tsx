export function StatusPill({ status }: { status: string }) {
  const tone =
    status === "connected" || status === "ready" || status === "healthy"
      ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/10"
      : status === "disabled" || status === "not connected"
        ? "text-muted-foreground border-border bg-muted/40"
        : "text-amber-300 border-amber-500/40 bg-amber-500/10";
  return <span className={`rounded-full border px-2 py-0.5 text-[11px] ${tone}`}>{status}</span>;
}