import { ShieldCheckIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { useGrantConnectorToAgent } from "@/hooks/useConnectors";

type GrantMutation = ReturnType<typeof useGrantConnectorToAgent>;

export function ConnectionPanelGrantBar({
  agents,
  agentId,
  onAgentIdChange,
  selectedTools,
  connectionId,
  grant,
}: {
  agents: Array<{ id: string; display_name: string }>;
  agentId: string;
  onAgentIdChange: (agentId: string) => void;
  selectedTools: string[];
  connectionId: string;
  grant: GrantMutation;
}) {
  return (
    <div className="mt-4 flex flex-wrap items-center gap-2">
      <select
        className="h-8 min-w-56 rounded-md border border-input bg-background px-2 text-sm"
        value={agentId}
        onChange={(e) => onAgentIdChange(e.target.value)}
        aria-label="Agent"
      >
        <option value="">Select agent</option>
        {agents.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.display_name}
          </option>
        ))}
      </select>
      <Button
        size="sm"
        onClick={() =>
          grant.mutate({ connectionId, agentId, tools: selectedTools })
        }
        disabled={!agentId || selectedTools.length === 0 || grant.isPending}
      >
        <ShieldCheckIcon /> Grant
      </Button>
      {grant.isError && (
        <span className="text-xs text-destructive">
          {grant.error instanceof Error ? grant.error.message : "Grant failed"}
        </span>
      )}
    </div>
  );
}