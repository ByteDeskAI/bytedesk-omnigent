import { useState } from "react";
import { Composer } from "./Composer";
import { useChatStore } from "@/store/chatStore";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

interface AgentComposerProps {
  /** Agent every send/slash-command is routed to. */
  agentId: string;
  agents?: AvailableAgent[];
  agentsLoading?: boolean;
}

/**
 * Thin chatStore-wired wrapper over the shared Composer for embedded chat.
 * No effort/model controls and a single fixed target agent — the host page
 * owns agent selection, so the picker is informational only.
 */
export function AgentComposer({ agentId, agents, agentsLoading = false }: AgentComposerProps) {
  const status = useChatStore((s) => s.status);
  const isWorking = status === "streaming";
  // Reply-quote state is local: embedded chat has no selection popup, but
  // the Composer still requires the props.
  const [replyQuotes, setReplyQuotes] = useState<string[]>([]);

  return (
    <Composer
      disabled={false}
      status={status}
      isWorking={isWorking}
      onSend={(text, files) => void useChatStore.getState().send(text, agentId, files)}
      onSendSlashCommand={(name, args) =>
        void useChatStore.getState().sendSlashCommand(name, args, agentId)
      }
      onStop={() => useChatStore.getState().stop()}
      agents={agents}
      agentsLoading={agentsLoading}
      selectedAgentId={agentId}
      onSelectAgent={() => {}}
      permissionLevel={null}
      readOnlyReason={null}
      replyQuotes={replyQuotes}
      onRemoveQuote={(i) => setReplyQuotes((prev) => prev.filter((_, idx) => idx !== i))}
      onClearAllQuotes={() => setReplyQuotes([])}
      effortLevels={[]}
      showEffort={false}
      showModels={false}
    />
  );
}
