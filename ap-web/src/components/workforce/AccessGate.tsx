import { ShieldAlertIcon } from "lucide-react";
import type { ReactNode } from "react";

export function AccessGate({ allowed, children }: { allowed: boolean | null; children: ReactNode }) {
  if (allowed === null) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }
  if (!allowed) {
    return (
      <div className="flex min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto mt-14 w-full max-w-5xl px-6">
          <div className="mc-fade-up mc-surface flex items-start gap-3 p-5">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-md border border-accent-red/40 bg-accent-red/10 text-accent-red">
              <ShieldAlertIcon className="size-4" />
            </span>
            <div>
              <h1 className="text-2xl font-semibold">Work Force</h1>
              <p className="mt-2 text-sm text-muted-foreground">
                You don't have permission to manage agents.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }
  return children;
}