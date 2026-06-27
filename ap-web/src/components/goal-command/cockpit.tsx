import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

// Shared cockpit primitives for the Goals Command Center (BDP-2598).
// Token-driven only — every color/border comes from a shadcn-mapped
// Tailwind utility (bg-*, border-*, text-*), never a literal — so the
// embed scope rewrite (:root → .omnigent-app) carries them unchanged.

export function CockpitCard({
  title,
  icon,
  action,
  children,
  className,
}: {
  title: string;
  icon?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("flex min-h-0 flex-col rounded-md border border-border", className)}>
      <header className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          {icon && <span className="text-muted-foreground">{icon}</span>}
          <h2 className="truncate text-sm font-semibold">{title}</h2>
        </div>
        {action}
      </header>
      <div className="min-h-0 flex-1 overflow-auto p-3">{children}</div>
    </section>
  );
}

export function CockpitEmpty({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-24 items-center justify-center rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

export function CockpitError({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
    >
      {children}
    </div>
  );
}

export function CockpitLoading({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-border px-3 py-4 text-sm text-muted-foreground">
      {children}
    </div>
  );
}

/** USD from integer cents, e.g. 12345 → "$123.45". */
export function formatCents(cents: number): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
  }).format(cents / 100);
}

/** Compact percent from a 0–1 confidence, e.g. 0.62 → "62%". */
export function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function formatTime(epochSeconds: number): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(epochSeconds * 1000));
}
