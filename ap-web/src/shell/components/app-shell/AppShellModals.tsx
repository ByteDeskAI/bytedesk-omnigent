import { PermissionsModal } from "@/components/PermissionsModal";
import { ForkSessionDialog } from "../../ForkSessionDialog";
import { AppShellAgentInfoDialog } from "./AppShellAgentInfoDialog";
import type { useAppShellState } from "./useAppShellState";

type AppShellModalsProps = Pick<
  ReturnType<typeof useAppShellState>,
  | "conversationId"
  | "shareOpen"
  | "setShareOpen"
  | "forkOpen"
  | "setForkOpen"
  | "forkUpToResponseId"
  | "setForkUpToResponseId"
  | "agentInfoOpen"
  | "setAgentInfoOpen"
  | "activeSession"
  | "boundAgent"
  | "hasAgentInfo"
>;

export function AppShellModals({
  conversationId,
  shareOpen,
  setShareOpen,
  forkOpen,
  setForkOpen,
  forkUpToResponseId,
  setForkUpToResponseId,
  agentInfoOpen,
  setAgentInfoOpen,
  activeSession,
  boundAgent,
  hasAgentInfo,
}: AppShellModalsProps) {
  return (
    <>
      {conversationId && (
        <PermissionsModal
          sessionId={conversationId}
          open={shareOpen}
          onOpenChange={setShareOpen}
        />
      )}
      {conversationId && (
        <ForkSessionDialog
          key={`fork-session-dialog-${conversationId}`}
          sourceSessionId={conversationId}
          sourceTitle={activeSession?.title}
          sourceWorkspace={activeSession?.workspace}
          sourceHostId={activeSession?.hostId}
          sourceGitBranch={activeSession?.gitBranch}
          upToResponseId={forkUpToResponseId}
          open={forkOpen}
          onOpenChange={(open) => {
            setForkOpen(open);
            if (!open) setForkUpToResponseId(null);
          }}
        />
      )}
      {hasAgentInfo && (
        <AppShellAgentInfoDialog
          open={agentInfoOpen}
          onOpenChange={setAgentInfoOpen}
          boundAgent={boundAgent}
          conversationId={conversationId}
        />
      )}
    </>
  );
}