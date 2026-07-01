import { RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ConnectionPanelAgentActions } from "./connection-panel/ConnectionPanelAgentActions";
import { ConnectionPanelGrantBar } from "./connection-panel/ConnectionPanelGrantBar";
import { ConnectionPanelHealthAlert } from "./connection-panel/ConnectionPanelHealthAlert";
import { ConnectionPanelServices } from "./connection-panel/ConnectionPanelServices";
import type { ConnectionPanelProps } from "./connection-panel/types";
import { useConnectionPanelState } from "./connection-panel/useConnectionPanelState";
import { StatusPill } from "./StatusPill";

export function ConnectionPanel(props: ConnectionPanelProps) {
  const { connection } = props;
  const {
    toggle,
    health,
    grant,
    agents,
    agentId,
    setAgentId,
    serviceGroups,
    actionGroups,
    availableTools,
    selectedTools,
    healthResult,
    requiredScopes,
    clientId,
    setToolSelected,
    setToolsSelected,
  } = useConnectionPanelState(props);

  const showHealthAlert = Boolean(connection.lastError || healthResult?.ok === false);

  return (
    <div className="rounded-md border border-border bg-background p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-medium">{connection.displayName}</h3>
            <StatusPill status={connection.status} />
            {connection.lastHealthStatus && <StatusPill status={connection.lastHealthStatus} />}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {connection.authType} · {connection.services.length} services ·{" "}
            {connection.grants.length} tool grants
          </div>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={() =>
            health.mutate({
              connectionId: connection.id,
              live: connection.provider === "google_workspace",
            })
          }
          disabled={health.isPending}
        >
          <RefreshCwIcon /> Test
        </Button>
      </div>

      {showHealthAlert && (
        <ConnectionPanelHealthAlert
          message={connection.lastError ?? "Connector health check failed"}
          clientId={clientId}
          requiredScopes={requiredScopes}
        />
      )}

      <ConnectionPanelServices
        connectionId={connection.id}
        serviceGroups={serviceGroups}
        toggle={toggle}
      />

      <ConnectionPanelAgentActions
        actionGroups={actionGroups}
        availableTools={availableTools}
        selectedTools={selectedTools}
        onToolSelected={setToolSelected}
        onToolsSelected={setToolsSelected}
      />

      <ConnectionPanelGrantBar
        agents={agents}
        agentId={agentId}
        onAgentIdChange={setAgentId}
        selectedTools={selectedTools}
        connectionId={connection.id}
        grant={grant}
      />
    </div>
  );
}