import { WorkspacePanelContent } from "./WorkspacePanelContent";
import { WorkspacePanelTabStrip } from "./WorkspacePanelTabStrip";
import { type RightRailTab } from "../../railTabs";
import type { ChangedSort } from "../../FlatFileList";

interface WorkspacePanelProps {
  conversationId: string;
  width: number;
  inert?: boolean;
  handleProps: React.HTMLAttributes<HTMLDivElement> & { tabIndex: number };
  rightRailTab: RightRailTab;
  onRightRailTabChange: (next: RightRailTab) => void;
  showFilesPanel: boolean;
  changedCount: number;
  showShellsTab: boolean;
  showBlueprintTab: boolean;
  blueprintNodeCount: number;
  boundAgentId: string | null;
  terminalsLength: number;
  subagentsWorking: number;
  agentCount: number;
  isClaudeNative: boolean;
  todosCompleted: number;
  todosTotal: number;
  rootSessionId: string | null;
  selectedFilePath: string | null;
  openFiles: string[];
  openFileViewer: (path: string) => void;
  onCloseFile: (path: string) => void;
  onShowScopeView: () => void;
  onCommentsOpenChange: (open: boolean) => void;
  openTerminalsPanel: (key: string) => void;
  permissionLevel: number | null;
  filesPanelSort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
  filesPanelFlatView: boolean;
  onFlatViewChange: (flat: boolean) => void;
  filesPanelShowHidden: boolean;
  onShowHiddenChange: (show: boolean) => void;
}

export function WorkspacePanel({
  conversationId,
  width,
  handleProps,
  inert,
  rightRailTab,
  onRightRailTabChange,
  showFilesPanel,
  changedCount,
  showShellsTab,
  showBlueprintTab,
  blueprintNodeCount,
  boundAgentId,
  terminalsLength,
  subagentsWorking,
  agentCount,
  isClaudeNative,
  todosCompleted,
  todosTotal,
  rootSessionId,
  selectedFilePath,
  openFiles,
  openFileViewer,
  onCloseFile,
  onShowScopeView,
  onCommentsOpenChange,
  openTerminalsPanel,
  permissionLevel,
  filesPanelSort,
  onSortChange,
  filesPanelFlatView,
  onFlatViewChange,
  filesPanelShowHidden,
  onShowHiddenChange,
}: WorkspacePanelProps) {
  return (
    <aside
      aria-label="Workspace"
      inert={inert}
      className="@container/rail relative z-40 hidden md:flex md:shrink-0 md:flex-col md:overflow-hidden md:mt-14 md:mr-2 md:mb-2 md:rounded-xl md:border md:border-border md:bg-card md:shadow-lg md:min-h-0"
      style={{ width }}
    >
      <div
        {...handleProps}
        className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
      />
      <WorkspacePanelTabStrip
        rightRailTab={rightRailTab}
        onRightRailTabChange={onRightRailTabChange}
        selectedFilePath={selectedFilePath}
        showFilesPanel={showFilesPanel}
        changedCount={changedCount}
        subagentsWorking={subagentsWorking}
        agentCount={agentCount}
        showBlueprintTab={showBlueprintTab}
        blueprintNodeCount={blueprintNodeCount}
        showShellsTab={showShellsTab}
        terminalsLength={terminalsLength}
        isClaudeNative={isClaudeNative}
        todosCompleted={todosCompleted}
        todosTotal={todosTotal}
        openFiles={openFiles}
        openFileViewer={openFileViewer}
        onCloseFile={onCloseFile}
      />
      <WorkspacePanelContent
        conversationId={conversationId}
        selectedFilePath={selectedFilePath}
        rightRailTab={rightRailTab}
        rootSessionId={rootSessionId}
        showBlueprintTab={showBlueprintTab}
        boundAgentId={boundAgentId}
        isClaudeNative={isClaudeNative}
        showShellsTab={showShellsTab}
        showFilesPanel={showFilesPanel}
        openFileViewer={openFileViewer}
        onShowScopeView={onShowScopeView}
        onCommentsOpenChange={onCommentsOpenChange}
        openTerminalsPanel={openTerminalsPanel}
        permissionLevel={permissionLevel}
        filesPanelSort={filesPanelSort}
        onSortChange={onSortChange}
        filesPanelFlatView={filesPanelFlatView}
        onFlatViewChange={onFlatViewChange}
        filesPanelShowHidden={filesPanelShowHidden}
        onShowHiddenChange={onShowHiddenChange}
      />
    </aside>
  );
}