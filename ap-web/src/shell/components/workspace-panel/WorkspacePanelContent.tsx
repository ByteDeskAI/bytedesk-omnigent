import { BlueprintPanel } from "../../BlueprintPanel";
import { FilesPanel } from "../../FilesPanel";
import { FileViewer } from "../../FileViewer";
import type { ChangedSort } from "../../FlatFileList";
import { InlineTerminalsSection } from "../../InlineTerminalsSection";
import { SubagentsPanel } from "../../SubagentsPanel";
import { TodoPanel } from "../../TodoPanel";
import { type RightRailTab } from "../../railTabs";

export function WorkspacePanelContent({
  conversationId,
  selectedFilePath,
  rightRailTab,
  rootSessionId,
  showBlueprintTab,
  boundAgentId,
  isClaudeNative,
  showShellsTab,
  showFilesPanel,
  openFileViewer,
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
}: {
  conversationId: string;
  selectedFilePath: string | null;
  rightRailTab: RightRailTab;
  rootSessionId: string | null;
  showBlueprintTab: boolean;
  boundAgentId: string | null;
  isClaudeNative: boolean;
  showShellsTab: boolean;
  showFilesPanel: boolean;
  openFileViewer: (path: string) => void;
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
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      {selectedFilePath !== null ? (
        <FileViewer
          frameless
          open
          conversationId={conversationId}
          path={selectedFilePath}
          onClose={onShowScopeView}
          onNavigateTo={openFileViewer}
          permissionLevel={permissionLevel}
          onCommentsOpenChange={onCommentsOpenChange}
          sort={filesPanelSort}
        />
      ) : rightRailTab === "subagents" && rootSessionId ? (
        <SubagentsPanel conversationId={conversationId} rootSessionId={rootSessionId} />
      ) : rightRailTab === "blueprint" && showBlueprintTab ? (
        <BlueprintPanel conversationId={conversationId} agentId={boundAgentId} />
      ) : rightRailTab === "todos" && isClaudeNative ? (
        <TodoPanel frameless />
      ) : rightRailTab === "terminals" && showShellsTab ? (
        <InlineTerminalsSection conversationId={conversationId} onExpand={openTerminalsPanel} />
      ) : (
        showFilesPanel && (
          <FilesPanel
            frameless
            onFileSelect={openFileViewer}
            flatView={filesPanelFlatView}
            onFlatViewChange={onFlatViewChange}
            showHidden={filesPanelShowHidden}
            onShowHiddenChange={onShowHiddenChange}
            sort={filesPanelSort}
            onSortChange={onSortChange}
          />
        )
      )}
    </div>
  );
}