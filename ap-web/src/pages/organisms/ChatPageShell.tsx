import { type Agent } from "@/hooks/useAgents";
import type { CostRoutingVerdict } from "@/components/CostRoutingControl";
import { MainAgentSurface } from "@/components/chat/MainAgentSurface";
import { SessionLayout } from "@/components/chat/SessionLayout";
import { HydratingPlaceholder } from "@/components/chat/HydratingPlaceholder";
import { ConversationLoadError } from "@/components/chat/ConversationLoadError";
import { SessionSharedContext } from "@/components/chat/SessionSharedContext";
import {
  effortLevelsForConv,
  shouldShowEffortPicker,
  shouldShowModelPicker,
} from "@/components/chat/chat-utils";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";
import { ResumeWithDirectoryDialog } from "@/shell/ResumeWithDirectoryDialog";
import { ReconnectSessionDialog } from "@/shell/ReconnectSessionDialog";
import { getCliServerUrl } from "@/lib/host";
import type { Bubble } from "@/lib/renderItems";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";

export type ChatPageShellProps =
  | { kind: "hydrating" }
  | { kind: "error"; conversationId: string; error: Error }
  | { kind: "landing" }
  | {
      kind: "session";
      urlConvId: string;
      isSessionShared: boolean;
      bubbles: Bubble[];
      status: "idle" | "streaming";
      isWorking: boolean;
      showsWorking: boolean;
      runnerOnline: boolean | undefined;
      liveness: SessionLiveness;
      agentsError: Error | null;
      disabled: boolean;
      onSend: (text: string, files?: File[]) => void;
      onSendSlashCommand: (name: string, args: string) => void;
      onStop: () => void;
      onShowReconnectHelp: () => void;
      visibleAgents: Agent[] | undefined;
      agentsLoading: boolean;
      agentId: string | null;
      onSelectAgent: (id: string | null) => void;
      hasMoreHistory: boolean;
      loadingMoreHistory: boolean;
      permissionLevel: number | null;
      readOnlyReason: string | null;
      capabilitySource: { labels: Record<string, string> };
      costRoutingVerdict: CostRoutingVerdict | null;
      costRoutingEligible: boolean;
      subAgentLabel: string | null;
      reconnectDialogOpen: boolean;
      setReconnectDialogOpen: (open: boolean) => void;
      reconnectState: "host_offline" | "local_stranded";
      reconnectIsOwner: boolean;
      activeConvTitle?: string | null;
      activeSessionTitle?: string | null;
      activeSessionWorkspace?: string | null;
      activeSessionHostId?: string | null;
      activeSessionGitBranch?: string | null;
      activeConvWrapper?: string | null;
      isUnboundFork: boolean;
      forkSourceId: string | null;
      resumeDirDialogOpen: boolean;
      setResumeDirDialogOpen: (open: boolean) => void;
    };

export function ChatPageShell(props: ChatPageShellProps) {
  if (props.kind === "hydrating") return <HydratingPlaceholder />;
  if (props.kind === "error") {
    return <ConversationLoadError conversationId={props.conversationId} error={props.error} />;
  }
  if (props.kind === "landing") return <NewChatLandingScreen />;

  const {
    urlConvId,
    isSessionShared,
    bubbles,
    status,
    isWorking,
    showsWorking,
    runnerOnline,
    liveness,
    agentsError,
    disabled,
    onSend,
    onSendSlashCommand,
    onStop,
    onShowReconnectHelp,
    visibleAgents,
    agentsLoading,
    agentId,
    onSelectAgent,
    hasMoreHistory,
    loadingMoreHistory,
    permissionLevel,
    readOnlyReason,
    capabilitySource,
    costRoutingVerdict,
    costRoutingEligible,
    subAgentLabel,
    reconnectDialogOpen,
    setReconnectDialogOpen,
    reconnectState,
    reconnectIsOwner,
    activeConvTitle,
    activeSessionTitle,
    activeSessionWorkspace,
    activeSessionHostId,
    activeSessionGitBranch,
    activeConvWrapper,
    isUnboundFork,
    forkSourceId,
    resumeDirDialogOpen,
    setResumeDirDialogOpen,
  } = props;

  const mainAgent = (
    <MainAgentSurface
      conversationId={urlConvId}
      bubbles={bubbles}
      status={status}
      isWorking={isWorking}
      showsWorking={showsWorking}
      runnerOnline={runnerOnline}
      liveness={liveness}
      agentsError={agentsError}
      disabled={disabled}
      onSend={onSend}
      onSendSlashCommand={onSendSlashCommand}
      onStop={onStop}
      onShowReconnectHelp={onShowReconnectHelp}
      agents={visibleAgents}
      agentsLoading={agentsLoading}
      selectedAgentId={agentId}
      onSelectAgent={onSelectAgent}
      hasMoreHistory={hasMoreHistory}
      loadingMoreHistory={loadingMoreHistory}
      permissionLevel={permissionLevel}
      readOnlyReason={readOnlyReason}
      effortLevels={effortLevelsForConv(capabilitySource)}
      showEffort={shouldShowEffortPicker(capabilitySource)}
      showModels={shouldShowModelPicker(capabilitySource)}
      costRoutingVerdict={costRoutingVerdict}
      costRoutingEligible={costRoutingEligible}
      subAgentLabel={subAgentLabel}
    />
  );

  return (
    <SessionSharedContext.Provider value={isSessionShared}>
      <SessionLayout mainAgent={mainAgent} />
      <ReconnectSessionDialog
        open={reconnectDialogOpen}
        onOpenChange={setReconnectDialogOpen}
        conversationId={urlConvId}
        serverUrl={getCliServerUrl()}
        wrapper={activeConvWrapper}
        state={reconnectState}
        isOwner={reconnectIsOwner}
        sourceTitle={activeConvTitle ?? activeSessionTitle}
        sourceWorkspace={activeSessionWorkspace}
        sourceHostId={activeSessionHostId}
        sourceGitBranch={activeSessionGitBranch}
      />
      {isUnboundFork && forkSourceId && (
        <ResumeWithDirectoryDialog
          open={resumeDirDialogOpen}
          onOpenChange={setResumeDirDialogOpen}
          sessionId={urlConvId}
          sourceSessionId={forkSourceId}
          serverUrl={getCliServerUrl()}
          wrapper={activeConvWrapper}
        />
      )}
    </SessionSharedContext.Provider>
  );
}