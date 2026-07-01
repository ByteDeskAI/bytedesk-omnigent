import { isOwnerLevel } from "@/lib/permissionsApi";
import { MainTerminalView } from "@/shell/MainTerminalView";
import { Composer } from "../Composer";
import { ConnectionIndicator } from "../ConnectionIndicator";
import { SelectionPopup } from "../SelectionPopup";
import { MainAgentConversationPane } from "./MainAgentConversationPane";
import type { MainAgentSurfaceProps } from "./types";
import { useMainAgentSurfaceState } from "./useMainAgentSurfaceState";

export function MainAgentSurface(props: MainAgentSurfaceProps) {
  const {
    conversationId,
    bubbles,
    status,
    isWorking,
    showsWorking,
    runnerOnline,
    liveness,
    agentsError,
    disabled,
    onStop,
    onShowReconnectHelp,
    agents,
    agentsLoading,
    selectedAgentId,
    onSelectAgent,
    hasMoreHistory,
    loadingMoreHistory,
    permissionLevel,
    readOnlyReason,
    effortLevels,
    showEffort,
    showModels,
    costRoutingVerdict,
    costRoutingEligible,
    subAgentLabel,
    onSend,
    onSendSlashCommand,
  } = props;

  const state = useMainAgentSurfaceState({
    conversationId,
    bubbles,
    showsWorking,
    runnerOnline,
    onSend,
    onSendSlashCommand,
  });

  if (state.showTerminal && conversationId) {
    return (
      <>
        <MainTerminalView
          conversationId={conversationId}
          initialTerminalKey={state.terminalFirst?.terminalViewKey}
          readOnly={!isOwnerLevel(permissionLevel)}
        />
        <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />
      </>
    );
  }

  return (
    <>
      <MainAgentConversationPane
        setConversationEl={state.setConversationEl}
        containerEl={state.containerEl}
        scroller={state.scroller}
        onScroller={state.setScroller}
        sendScrollNonce={state.sendScrollNonce}
        bubbles={bubbles}
        showWorkingIndicator={state.showWorkingIndicator}
        launching={state.launching}
        agentsError={agentsError}
        hasMoreHistory={hasMoreHistory}
        loadingMoreHistory={loadingMoreHistory}
        subAgentLabel={subAgentLabel}
        nav={state.nav}
        userMessageIds={state.userMessageIds}
      />
      <SelectionPopup
        containerRef={state.conversationRef}
        onReply={(text) => state.setReplyQuotes((prev) => [...prev, text])}
      />
      <Composer
        disabled={disabled}
        status={status}
        isWorking={isWorking}
        onSend={state.handleSend}
        onSendSlashCommand={state.handleSendSlashCommand}
        onStop={onStop}
        agents={agents}
        agentsLoading={agentsLoading}
        selectedAgentId={selectedAgentId}
        onSelectAgent={onSelectAgent}
        permissionLevel={permissionLevel}
        readOnlyReason={readOnlyReason}
        replyQuotes={state.replyQuotes}
        onRemoveQuote={(i) => state.setReplyQuotes((prev) => prev.filter((_, idx) => idx !== i))}
        onClearAllQuotes={() => state.setReplyQuotes([])}
        effortLevels={effortLevels}
        showEffort={showEffort}
        showModels={showModels}
        isTerminalFirst={state.isTerminalFirst}
        isNativeWrapper={state.isNativeWrapper}
        reconnectHint={liveness.kind === "runner_asleep"}
        unreachable={
          !state.sandboxLaunching &&
          (liveness.kind === "host_offline" || liveness.kind === "local_stranded")
        }
        costRoutingVerdict={costRoutingVerdict}
        costRoutingEligible={costRoutingEligible}
        subAgentLabel={subAgentLabel}
      />
      <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />
    </>
  );
}