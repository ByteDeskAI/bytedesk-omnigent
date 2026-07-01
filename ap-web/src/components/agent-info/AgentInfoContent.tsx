import type { Agent } from "@/hooks/useAgents";
import { useChatStore } from "@/store/chatStore";
import { agentDisplayLabel, formatSessionCostUsd } from "./agent-info-utils";
import { McpServerList } from "./McpServerList";
import { ModelUsageBreakdown } from "./ModelUsageBreakdown";
import { SectionLabel } from "./SectionLabel";
import { SessionPoliciesSection } from "./SessionPoliciesSection";

export interface AgentInfoProps {
  /** The bound agent for the active session. Undefined while loading. */
  agent: Agent | undefined;
  /** Session ID — needed to manage user policies. */
  sessionId?: string | null;
}

export function agentHasInfo(agent: Agent | undefined, sessionId?: string | null): boolean {
  return !!sessionId || (agent?.mcp_servers?.length ?? 0) > 0;
}

export function AgentInfoContent({ agent, sessionId }: AgentInfoProps) {
  const servers = agent?.mcp_servers ?? [];
  const displayName = agent ? (agent.display_name ?? agentDisplayLabel(agent.name)) : null;
  const sessionCostUsd = useChatStore((s) => s.sessionCostUsd);
  const usageByModel = useChatStore((s) => s.sessionUsageByModel);

  return (
    <div className="flex flex-col gap-3">
      {displayName && (
        <div className="flex flex-col gap-0.5">
          <span className="font-medium text-sm">{displayName}</span>
          {agent?.description && (
            <span className="text-xs text-muted-foreground">{agent.description}</span>
          )}
        </div>
      )}
      {sessionId && sessionCostUsd != null && (
        <div className="flex flex-col gap-1.5">
          <SectionLabel>Session cost</SectionLabel>
          <span
            className="text-sm tabular-nums text-muted-foreground"
            data-testid="agent-info-session-cost"
          >
            {formatSessionCostUsd(sessionCostUsd)}
          </span>
        </div>
      )}
      {sessionId && usageByModel != null && Object.keys(usageByModel).length > 0 && (
        <ModelUsageBreakdown usageByModel={usageByModel} />
      )}
      {servers.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <SectionLabel>Tools</SectionLabel>
          <McpServerList servers={servers} />
        </div>
      )}
      {sessionId && <SessionPoliciesSection sessionId={sessionId} />}
    </div>
  );
}