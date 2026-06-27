import { Loader2Icon, MessageSquareIcon } from "lucide-react";
import { ConversationEmptyState } from "@/components/ai-elements/conversation";
import { AgentComposer, AgentConversation } from "@/components/chat";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { CockpitError } from "./cockpit";

// The "set / drive" half: the founder converses with the goal-commander
// to create, approve, prioritize, adjust budget, and arm — every goal
// mutation is a conversation, not a form. Reuses the embedded chat surface
// (AgentConversation + AgentComposer) bound to the persistent commander
// session via the shared chatStore.
export function CommanderChat({
  agentId,
  sessionId,
  starting,
  error,
}: {
  agentId: string | null;
  sessionId: string | null;
  starting: boolean;
  error: string | null;
}) {
  const agents = useAvailableAgents();

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-4">
        <CockpitError>{error}</CockpitError>
      </div>
    );
  }

  if (!agentId || !sessionId || starting) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-sm text-muted-foreground">
        <Loader2Icon className="size-5 animate-spin" />
        Connecting to the goal commander…
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <AgentConversation
        emptyState={
          <ConversationEmptyState
            icon={<MessageSquareIcon className="size-5" />}
            title="Goal commander"
            description="Set, approve, prioritize, budget, and arm goals — just ask."
          />
        }
      />
      <div className="shrink-0 border-t border-border">
        <AgentComposer agentId={agentId} agents={agents.data} agentsLoading={agents.isLoading} />
      </div>
    </div>
  );
}
