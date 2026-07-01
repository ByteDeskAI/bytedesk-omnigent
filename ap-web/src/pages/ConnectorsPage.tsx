import { PlugIcon, RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  ConnectorAccessGate,
  ConnectorBreadcrumbs,
  ConnectorHeader,
  ConnectorPageShell,
  ProviderCatalogCard,
  ProviderDetail,
  useConnectorAdminAccess,
} from "@/components/connectors";
import { useConnectorsCatalog } from "@/hooks/useConnectors";
import { Link, useParams } from "@/lib/routing";

export function ConnectorsPage() {
  const allowed = useConnectorAdminAccess();
  const { data = [], isLoading, isError, error, refetch } = useConnectorsCatalog();

  return (
    <ConnectorAccessGate allowed={allowed}>
      <ConnectorPageShell>
        <ConnectorBreadcrumbs />
        <ConnectorHeader
          title="Connectors"
          description="External service access for Omnigent agents."
          icon={<PlugIcon className="mt-1 size-5 shrink-0 text-muted-foreground" />}
          action={
            <Button variant="ghost" size="icon" onClick={() => void refetch()}>
              <RefreshCwIcon />
            </Button>
          }
        />

        {isError && (
          <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error instanceof Error ? error.message : "Failed to load connectors."}
          </div>
        )}
        {isLoading && <div className="text-sm text-muted-foreground">Loading connectors...</div>}
        {!isLoading && data.length === 0 && (
          <div className="text-sm text-muted-foreground">
            No connector manifests are registered.
          </div>
        )}
        <div className="flex flex-col gap-4">
          {data.map((provider) => (
            <ProviderCatalogCard key={provider.provider} provider={provider} />
          ))}
        </div>
      </ConnectorPageShell>
    </ConnectorAccessGate>
  );
}

export function ConnectorDetailPage() {
  const allowed = useConnectorAdminAccess();
  const { provider: providerParam } = useParams<{ provider: string }>();
  const { data = [], isLoading, isError, error, refetch } = useConnectorsCatalog();
  const providerKey = providerParam ?? "";
  const provider = data.find((item) => item.provider === providerKey);

  return (
    <ConnectorAccessGate allowed={allowed}>
      <ConnectorPageShell>
        {isError && (
          <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error instanceof Error ? error.message : "Failed to load connectors."}
          </div>
        )}
        {isLoading && <div className="text-sm text-muted-foreground">Loading connectors...</div>}
        {!isLoading && !provider && (
          <>
            <ConnectorBreadcrumbs providerName="Not found" />
            <div className="max-w-2xl">
              <h1 className="mb-2 text-2xl font-semibold">Connector not found</h1>
              <p className="mb-4 text-sm text-muted-foreground">
                No connector manifest is registered for {providerKey || "this provider"}.
              </p>
              <Button asChild variant="outline">
                <Link to="/connectors">Back to connectors</Link>
              </Button>
            </div>
          </>
        )}
        {provider && <ProviderDetail provider={provider} providers={data} refetch={refetch} />}
      </ConnectorPageShell>
    </ConnectorAccessGate>
  );
}