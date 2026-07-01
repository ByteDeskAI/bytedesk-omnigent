import { useMemo } from "react";
import { Button } from "@/components/ui/button";
import { useStartConnectorOAuth } from "@/hooks/useConnectors";
import type { ConnectorManifest } from "@/lib/connectorsApi";
import { buildConnectorPresentation } from "@/lib/connectorPresentation";
import { Link } from "@/lib/routing";
import { ConnectorMetric } from "./ConnectorMetric";
import { StatusPill } from "./StatusPill";

export function ProviderCatalogCard({ provider }: { provider: ConnectorManifest }) {
  const startOAuth = useStartConnectorOAuth();
  const presentation = useMemo(() => buildConnectorPresentation(provider), [provider]);
  const connected = provider.connections.length > 0;

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold">{provider.name}</h2>
            <StatusPill status={presentation.summary.status} />
            <StatusPill status={provider.auth.type} />
          </div>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{provider.description}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <ConnectorMetric label="connections" value={presentation.summary.connectionCount} />
            <ConnectorMetric label="services" value={presentation.summary.serviceCount} />
            <ConnectorMetric label="actions" value={presentation.summary.actionCount} />
            <ConnectorMetric label="grants" value={presentation.summary.grantCount} />
          </div>
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          {!connected && provider.auth.type === "oauth_3lo" && (
            <Button
              size="sm"
              onClick={() => startOAuth.mutate(provider.provider)}
              disabled={startOAuth.isPending}
            >
              Connect
            </Button>
          )}
          <Button asChild size="sm" variant="outline">
            <Link to={`/connectors/${encodeURIComponent(provider.provider)}`}>Configure</Link>
          </Button>
        </div>
      </div>

      {startOAuth.isError && (
        <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {startOAuth.error instanceof Error ? startOAuth.error.message : "OAuth start failed"}
        </div>
      )}
    </section>
  );
}