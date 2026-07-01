import type { ReactNode } from "react";

export function ConnectorPageShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 overflow-y-auto" data-testid="connector-page-scroll">
      <div className="mx-auto w-full max-w-6xl px-6 pt-14 pb-16">{children}</div>
    </div>
  );
}