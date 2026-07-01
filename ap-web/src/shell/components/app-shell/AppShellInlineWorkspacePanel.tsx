import { WorkspacePanel } from "../workspace-panel/WorkspacePanel";
import type { useAppShellState } from "./useAppShellState";

type AppShellInlineWorkspacePanelProps = Pick<
  ReturnType<typeof useAppShellState> & { setAgentInfoOpen?: (open: boolean) => void },
  | "conversationId"
  | "panelOpen"
  | "terminalFirst"
  | "executionLogsOpen"
  | "filesPanelOpen"
  | "hasRailContent"
  | "rightPanelOpen"
  | "inlinePanelWidth"
  | "inlinePanelHandleProps"
  | "rightRailTab"
  | "handleRightRailTabChange"
  | "showFilesPanel"
  | "changedCount"
  | "railTabsAvailable"
  | "blueprintNodeCount"
  | "boundAgent"
  | "railTerminals"
  | "subagentsWorking"
  | "agentCount"
  | "isClaudeNative"
  | "todos"
  | "todosCompleted"
  | "rootSessionId"
  | "selectedFilePath"
  | "openFiles"
  | "openFileViewer"
  | "closeFile"
  | "showScopeView"
  | "setFileViewerCommentsOpen"
  | "openTerminalsPanel"
  | "permissionLevel"
  | "filesPanelSort"
  | "setFilesPanelSort"
  | "filesPanelFlatView"
  | "handleFilesFlatViewChange"
  | "filesPanelShowHidden"
  | "setFilesPanelShowHidden"
>;

export function AppShellInlineWorkspacePanel(props: AppShellInlineWorkspacePanelProps) {
  const {
    conversationId,
    panelOpen,
    terminalFirst,
    executionLogsOpen,
    filesPanelOpen,
    hasRailContent,
    rightPanelOpen,
    inlinePanelWidth,
    inlinePanelHandleProps,
    rightRailTab,
    handleRightRailTabChange,
    showFilesPanel,
    changedCount,
    railTabsAvailable,
    blueprintNodeCount,
    boundAgent,
    railTerminals,
    subagentsWorking,
    agentCount,
    isClaudeNative,
    todos,
    todosCompleted,
    rootSessionId,
    selectedFilePath,
    openFiles,
    openFileViewer,
    closeFile,
    showScopeView,
    setFileViewerCommentsOpen,
    openTerminalsPanel,
    permissionLevel,
    filesPanelSort,
    setFilesPanelSort,
    filesPanelFlatView,
    handleFilesFlatViewChange,
    filesPanelShowHidden,
    setFilesPanelShowHidden,
  } = props;

  if (
    !conversationId ||
    !hasRailContent ||
    !rightPanelOpen ||
    (!terminalFirst && panelOpen) ||
    executionLogsOpen ||
    filesPanelOpen
  ) {
    return null;
  }

  return (
    <WorkspacePanel
      conversationId={conversationId}
      width={inlinePanelWidth}
      inert={inlinePanelWidth === 0}
      handleProps={inlinePanelHandleProps}
      rightRailTab={rightRailTab}
      onRightRailTabChange={handleRightRailTabChange}
      showFilesPanel={showFilesPanel}
      changedCount={changedCount}
      showShellsTab={railTabsAvailable.terminals}
      showBlueprintTab={railTabsAvailable.blueprint}
      blueprintNodeCount={blueprintNodeCount}
      boundAgentId={boundAgent?.id ?? null}
      terminalsLength={railTerminals.length}
      subagentsWorking={subagentsWorking}
      agentCount={agentCount}
      isClaudeNative={isClaudeNative}
      todosCompleted={todosCompleted}
      todosTotal={todos.length}
      rootSessionId={rootSessionId}
      selectedFilePath={selectedFilePath}
      openFiles={openFiles}
      openFileViewer={openFileViewer}
      onCloseFile={closeFile}
      onShowScopeView={showScopeView}
      onCommentsOpenChange={setFileViewerCommentsOpen}
      openTerminalsPanel={openTerminalsPanel}
      permissionLevel={permissionLevel}
      filesPanelSort={filesPanelSort}
      onSortChange={setFilesPanelSort}
      filesPanelFlatView={filesPanelFlatView}
      onFlatViewChange={handleFilesFlatViewChange}
      filesPanelShowHidden={filesPanelShowHidden}
      onShowHiddenChange={setFilesPanelShowHidden}
    />
  );
}