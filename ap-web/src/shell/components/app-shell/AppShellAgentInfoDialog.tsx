import { AgentInfoContent } from "@/components/AgentInfo";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Agent } from "@/hooks/useAgents";

interface AppShellAgentInfoDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  boundAgent: Agent | undefined;
  conversationId: string | undefined;
}

/** Agent tools & policies — mobile counterpart of the desktop AgentInfoButton popover. */
export function AppShellAgentInfoDialog({
  open,
  onOpenChange,
  boundAgent,
  conversationId,
}: AppShellAgentInfoDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Agent</DialogTitle>
          <DialogDescription className="sr-only">
            Tools and policies configured for the active agent.
          </DialogDescription>
        </DialogHeader>
        <AgentInfoContent agent={boundAgent} sessionId={conversationId} />
      </DialogContent>
    </Dialog>
  );
}