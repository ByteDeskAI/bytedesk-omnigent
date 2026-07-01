import type { ReactNode } from "react";

export function WorkForceShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <div className="grid min-h-0 w-full grid-cols-1 lg:grid-cols-[19rem_minmax(0,1fr)]">
        {children}
      </div>
    </div>
  );
}