import { useEffect, useMemo, useState } from "react";
import { ArrowLeftIcon, ExternalLinkIcon, PlugIcon, RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useStartConnectorOAuth } from "@/hooks/useConnectors";
import type { ConnectorManifest } from "@/lib/connectorsApi";
import { buildConnectorPresentation } from "@/lib/connectorPresentation";
import { Link } from "@/lib/routing";
import { ConnectorBreadcrumbs } from "./ConnectorBreadcrumbs";
import { ConnectorHeader } from "./ConnectorHeader";
import { ConnectorMetric } from "./ConnectorMetric";
import { ConnectionPanel } from "./ConnectionPanel";
import { ConnectionSelector } from "./ConnectionSelector";
import { CredentialSetup } from "./CredentialSetup";
import { ProviderSelector } from "./ProviderSelector";
import { StatusPill } from "./StatusPill";

export function ProviderDetail({
  provider,
  providers,
  refetch,
}: {
  provider: ConnectorManifest;
  providers: ConnectorManifest[];
  refetch: () => unknown;
}) {
  const startOAuth = useStartConnectorOAuth();
  const presentation = useMemo(() => buildConnectorPresentation(provider), [provider]);
  const [selectedConnectionId, setSelectedConnectionId] = useState(
    provider.connections[0]?.id ?? "",
  );

  useEffect(() => {
    if (provider.connections.length === 0) {
      setSelectedConnectionId("");
      return;
    }
    if (!provider.connections.some((connection) => connection.id === selectedConnectionId)) {
      setSelectedConnectionId(provider.connections[0]?.id ?? "");
    }
  }, [provider.connections, selectedConnectionId]);

  const selectedConnection =
    provider.connections.find((connection) => connection.id === selectedConnectionId) ??
    provider.connections[0];
  const connected = provider.connections.length > 0;

  return (
    <>
      <ConnectorBreadcrumbs providerName={provider.name} />
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link to="/connectors">
            <ArrowLeftIcon /> Back
          </Link>
        </Button>
        <ProviderSelector providers={providers} provider={provider} />
      </div>

      <ConnectorHeader
        title={provider.name}
        description={provider.description}
        icon={<PlugIcon className="mt-1 size-5 shrink-0 text-muted-foreground" />}
        action={
          <div className="flex flex-wrap justify-end gap-2">
            {provider.auth.docsUrl && (
              <Button asChild variant="ghost" size="sm">
                <a href={provider.auth.docsUrl} target="_blank" rel="noreferrer">
                  Docs <ExternalLinkIcon />
                </a>
              </Button>
            )}
            <Button variant="ghost" size="icon" onClick={() => void refetch()}>
              <RefreshCwIcon />
            </Button>
          </div>
        }
      />

      <div className="mb-4 flex flex-wrap gap-2">
        <StatusPill status={presentation.summary.status} />
        <StatusPill status={provider.auth.type} />
        <ConnectorMetric label="connections" value={presentation.summary.connectionCount} />
        <ConnectorMetric label="services" value={presentation.summary.serviceCount} />
        <ConnectorMetric label="actions" value={presentation.summary.actionCount} />
        <ConnectorMetric label="enabled services" value={presentation.summary.enabledServiceCount} />
        <ConnectorMetric label="grants" value={presentation.summary.grantCount} />
      </div>

      {startOAuth.isError && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {startOAuth.error instanceof Error ? startOAuth.error.message : "OAuth start failed"}
        </div>
      )}

      {!connected && provider.auth.type === "oauth_3lo" && (
        <div className="mb-4 rounded-md border border-border bg-card p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-medium">Connect {provider.name}</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Start the provider authorization flow before configuring services and grants.
              </p>
            </div>
            <Button onClick={() => startOAuth.mutate(provider.provider)} disabled={startOAuth.isPending}>
              Connect
            </Button>
          </div>
        </div>
      )}

      {!connected && provider.auth.type !== "oauth_3lo" && provider.auth.setupFields.length > 0 && (
        <div className="mb-4 rounded-md border border-border bg-card p-4">
          <h2 className="mb-3 font-medium">Connection setup</h2>
          <CredentialSetup provider={provider} />
        </div>
      )}

      {connected && (
        <div className="flex flex-col gap-4">
          <ConnectionSelector
            connections={provider.connections}
            selectedConnectionId={selectedConnection?.id ?? ""}
            onChange={setSelectedConnectionId}
          />
          {selectedConnection && (
            <ConnectionPanel
              key={selectedConnection.id}
              connection={selectedConnection}
              provider={provider}
              presentation={presentation}
            />
          )}
        </div>
      )}
    </>
  );
}