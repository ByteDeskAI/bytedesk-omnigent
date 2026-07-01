import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { cn } from "@/lib/utils";
import { PageTransitionOutlet } from "../../PageTransitionOutlet";
import { ChatHeader } from "../chat-header/ChatHeader";
import { AppShellInlineWorkspacePanel } from "./AppShellInlineWorkspacePanel";
import type { useAppShellState } from "./useAppShellState";

type AppShellChatRegionProps = Pick<
  ReturnType<typeof useAppShellState>,
  | "conversationId"
  | "sidebarOpen"
  | "setSidebarOpen"
  | "selectedFilePath"
  | "openFiles"
  | "filesPanelFlatView"
  | "filesPanelShowHidden"
  | "setFilesPanelShowHidden"
  | "filesPanelSort"
  | "setFilesPanelSort"
  | "panelOpen"
  | "filesPanelOpen"
  | "subagentsPanelOpen"
  | "todosPanelOpen"
  | "blueprintPanelOpen"
  | "setShareOpen"
  | "rightPanelOpen"
  | "setRightPanelOpen"
  | "activeSession"
  | "boundAgent"
  | "blueprintAvailable"
  | "blueprintNodeCount"
  | "permissionLevel"
  | "terminalFirst"
  | "isClaudeNative"
  | "todos"
  | "todosCompleted"
  | "isChildSession"
  | "canShare"
  | "hasAgentInfo"
  | "hasHeaderMenu"
  | "debugMode"
  | "hideTerminalsTab"
  | "railTerminals"
  | "rootSessionId"
  | "subagentsWorking"
  | "agentCount"
  | "showFilesPanel"
  | "railTabsAvailable"
  | "hasRailContent"
  | "changedCount"
  | "openFileViewer"
  | "handleFilesFlatViewChange"
  | "closeFile"
  | "handleRightRailTabChange"
  | "openTerminalsPanel"
  | "openFilesPanel"
  | "openSubagentsPanel"
  | "openBlueprintPanel"
  | "openTodosPanel"
  | "openFirstTerminal"
  | "openMainExecutionLog"
  | "clearFileViewerUrl"
  | "setSearchParams"
  | "setFileViewerCommentsOpen"
  | "fileViewerOpen"
  | "executionLogsOpen"
  | "rightRailTab"
  | "inlinePanelWidth"
  | "inlinePanelHandleProps"
  | "showScopeView"
> & {
  setAgentInfoOpen: (open: boolean) => void;
};

export function AppShellChatRegion(props: AppShellChatRegionProps) {
  const {
    conversationId,
    sidebarOpen,
    setSidebarOpen,
    selectedFilePath,
    panelOpen,
    filesPanelOpen,
    subagentsPanelOpen,
    todosPanelOpen,
    blueprintPanelOpen,
    setShareOpen,
    rightPanelOpen,
    setRightPanelOpen,
    activeSession,
    boundAgent,
    blueprintAvailable,
    blueprintNodeCount,
    terminalFirst,
    isClaudeNative,
    todos,
    todosCompleted,
    isChildSession,
    canShare,
    hasAgentInfo,
    hasHeaderMenu,
    debugMode,
    hideTerminalsTab,
    railTerminals,
    subagentsWorking,
    agentCount,
    showFilesPanel,
    hasRailContent,
    changedCount,
    openFilesPanel,
    openSubagentsPanel,
    openBlueprintPanel,
    openTodosPanel,
    openFirstTerminal,
    openMainExecutionLog,
    clearFileViewerUrl,
    setSearchParams,
    fileViewerOpen,
    executionLogsOpen,
    setAgentInfoOpen,
  } = props;

  return (
    <div
      className={cn(
        "relative flex min-h-0 min-w-0 flex-1",
        panelOpen && !terminalFirst && "md:hidden",
      )}
    >
      <ChatHeader
        sidebarOpen={sidebarOpen}
        onOpenSidebar={() => setSidebarOpen(true)}
        isChildSession={isChildSession}
        parentSessionId={activeSession?.parentSessionId}
        conversationId={conversationId}
        boundAgent={boundAgent}
        canShare={canShare}
        onShare={() => setShareOpen(true)}
        hasAgentInfo={hasAgentInfo}
        onAgentInfo={() => setAgentInfoOpen(true)}
        hasHeaderMenu={hasHeaderMenu}
        showFilesPanel={showFilesPanel}
        hasRailContent={hasRailContent}
        rightPanelOpen={rightPanelOpen}
        onToggleRightPanel={() => {
          const next = !rightPanelOpen;
          if (conversationId) writeSessionWorkspaceState(conversationId, { open: next });
          if (next) {
            if (selectedFilePath) {
              setSearchParams(
                (prev) => {
                  const params = new URLSearchParams(prev);
                  params.set("file", selectedFilePath);
                  return params;
                },
                { replace: true },
              );
            }
          } else {
            clearFileViewerUrl();
          }
          setRightPanelOpen(next);
        }}
        mobileMenu={{
          fileViewerOpen,
          panelOpen,
          terminalFirst,
          executionLogsOpen,
          filesPanelOpen,
          subagentsPanelOpen,
          blueprintPanelOpen,
          todosPanelOpen,
          hideTerminalsTab,
          terminalsLength: railTerminals.length,
          isClaudeNative,
          todosCompleted,
          todosTotal: todos.length,
          debugMode,
          changedCount,
          subagentsWorking,
          showBlueprintTab: blueprintAvailable,
          blueprintNodeCount,
          agentCount,
          onOpenFiles: openFilesPanel,
          onOpenFirstTerminal: openFirstTerminal,
          onOpenSubagents: openSubagentsPanel,
          onOpenBlueprint: openBlueprintPanel,
          onOpenTodos: openTodosPanel,
          onOpenMainExecutionLog: openMainExecutionLog,
        }}
      />
      <main className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        <PageTransitionOutlet />
      </main>
      <AppShellInlineWorkspacePanel {...props} />
    </div>
  );
}