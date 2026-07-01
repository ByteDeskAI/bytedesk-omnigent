import type { ReactNode } from "react";
import { ConnectorPageShell } from "./ConnectorPageShell";

export function ConnectorAccessGate({
  allowed,
  children,
}: {
  allowed: boolean | null;
  children: ReactNode;
}) {
  if (allowed === null) {
    return (
      <ConnectorPageShell>
        <div className="flex min-h-80 items-center justify-center text-sm text-muted-foreground">
          Loading...
        </div>
      </ConnectorPageShell>
    );
  }

  if (!allowed) {
    return (
      <ConnectorPageShell>
        <div className="max-w-2xl">
          <h1 className="mb-2 text-2xl font-semibold">Connectors</h1>
          <p className="text-sm text-muted-foreground">
            You don't have permission to manage connectors.
          </p>
        </div>
      </ConnectorPageShell>
    );
  }

  return children;
}