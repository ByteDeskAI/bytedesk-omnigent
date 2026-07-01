import { useState } from "react";
import { PlusIcon } from "lucide-react";
import { useChildSessions } from "@/hooks/useChildSessions";
import { AddAgentDialog } from "./AddAgentDialog";
import { SubagentMainRow } from "./components/SubagentMainRow";
import { SubagentRow } from "./components/SubagentRow";
import { TREE_POLL_MS } from "./components/subagentsPanelConstants";

export { iconForAgentType } from "./components/subagentsPanelUtils";

interface SubagentsPanelProps {
  conversationId: string;
  rootSessionId: string;
}

export function SubagentsPanel({ conversationId, rootSessionId }: SubagentsPanelProps) {
  const { children, isLoading, error } = useChildSessions(rootSessionId, TREE_POLL_MS);
  const [addOpen, setAddOpen] = useState(false);

  if (isLoading && children.length === 0) {
    return (
      <div className="flex h-full flex-1 items-center justify-center px-4 py-8 text-center text-xs text-muted-foreground bg-card">
        Loading…
      </div>
    );
  }
  if (error && children.length === 0) {
    return (
      <div className="flex h-full flex-1 items-center justify-center px-4 py-8 text-center text-xs text-muted-foreground bg-card">
        Failed to load agents.
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      <button
        type="button"
        data-testid="add-agent-button"
        onClick={() => setAddOpen(true)}
        className="hidden"
      >
        <PlusIcon className="size-3.5 shrink-0" />
        Add agent
      </button>
      <ul className="flex min-h-0 flex-1 flex-col overflow-y-auto pb-1">
        <SubagentMainRow rootSessionId={rootSessionId} isActive={conversationId === rootSessionId} />
        {children.map((child) => (
          <SubagentRow key={child.id} child={child} depth={1} conversationId={conversationId} />
        ))}
      </ul>
      {addOpen && (
        <AddAgentDialog parentSessionId={rootSessionId} open={addOpen} onOpenChange={setAddOpen} />
      )}
    </div>
  );
}