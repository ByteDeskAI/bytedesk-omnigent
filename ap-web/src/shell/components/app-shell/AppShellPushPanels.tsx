import { isOwnerLevel } from "@/lib/permissionsApi";
import { ExecutionLogsPanel } from "../../ExecutionLogsPanel";
import { FileViewer } from "../../FileViewer";
import { FilesPanelDrawer } from "../../FilesPanelDrawer";
import { MobilePanelDrawer } from "../../MobilePanelDrawer";
import { SubagentsPanel } from "../../SubagentsPanel";
import { TerminalsPanel } from "../../TerminalsPanel";
import { TodoPanel } from "../../TodoPanel";
import { BlueprintPanel } from "../../BlueprintPanel";
import type { useAppShellState } from "./useAppShellState";

type AppShellPushPanelsProps = Pick<
  ReturnType<typeof useAppShellState>,
  | "conversationId"
  | "terminalFirst"
  | "panelOpen"
  | "panelInitialKey"
  | "setPanelInitialKey"
  | "executionLogsOpen"
  | "executionLogsKey"
  | "setExecutionLogsKey"
  | "filesPanelOpen"
  | "setFilesPanelOpen"
  | "subagentsPanelOpen"
  | "setSubagentsPanelOpen"
  | "blueprintPanelOpen"
  | "setBlueprintPanelOpen"
  | "todosPanelOpen"
  | "setTodosPanelOpen"
  | "showFilesPanel"
  | "filesPanelFlatView"
  | "handleFilesFlatViewChange"
  | "filesPanelShowHidden"
  | "setFilesPanelShowHidden"
  | "filesPanelSort"
  | "setFilesPanelSort"
  | "openFileViewer"
  | "selectedFilePath"
  | "closeFileViewer"
  | "permissionLevel"
  | "rootSessionId"
  | "boundAgent"
>;

export function AppShellPushPanels({
  conversationId,
  terminalFirst,
  panelOpen,
  panelInitialKey,
  setPanelInitialKey,
  executionLogsOpen,
  executionLogsKey,
  setExecutionLogsKey,
  filesPanelOpen,
  setFilesPanelOpen,
  subagentsPanelOpen,
  setSubagentsPanelOpen,
  blueprintPanelOpen,
  setBlueprintPanelOpen,
  todosPanelOpen,
  setTodosPanelOpen,
  showFilesPanel,
  filesPanelFlatView,
  handleFilesFlatViewChange,
  filesPanelShowHidden,
  setFilesPanelShowHidden,
  filesPanelSort,
  setFilesPanelSort,
  openFileViewer,
  selectedFilePath,
  closeFileViewer,
  permissionLevel,
  rootSessionId,
  boundAgent,
}: AppShellPushPanelsProps) {
  return (
    <>
      {conversationId && !terminalFirst && (
        <TerminalsPanel
          open={panelOpen}
          conversationId={conversationId}
          initialTerminalKey={panelInitialKey}
          fluid={panelOpen}
          readOnly={!isOwnerLevel(permissionLevel)}
          onClose={() => setPanelInitialKey(null)}
        />
      )}
      {conversationId && (
        <ExecutionLogsPanel
          open={executionLogsOpen}
          conversationId={conversationId}
          initialKey={executionLogsKey}
          onClose={() => setExecutionLogsKey(null)}
        />
      )}
      {conversationId && showFilesPanel && (
        <FilesPanelDrawer
          open={filesPanelOpen}
          onClose={() => setFilesPanelOpen(false)}
          onFileSelect={openFileViewer}
          flatView={filesPanelFlatView}
          onFlatViewChange={handleFilesFlatViewChange}
          showHidden={filesPanelShowHidden}
          onShowHiddenChange={setFilesPanelShowHidden}
          sort={filesPanelSort}
          onSortChange={setFilesPanelSort}
        />
      )}
      {conversationId && rootSessionId && (
        <MobilePanelDrawer
          open={subagentsPanelOpen}
          title="Agents"
          onClose={() => setSubagentsPanelOpen(false)}
          testId="subagents-panel-drawer"
        >
          <SubagentsPanel conversationId={conversationId} rootSessionId={rootSessionId} />
        </MobilePanelDrawer>
      )}
      {conversationId && (
        <MobilePanelDrawer
          open={blueprintPanelOpen}
          title="Blueprint"
          onClose={() => setBlueprintPanelOpen(false)}
          testId="blueprint-panel-drawer"
        >
          <BlueprintPanel conversationId={conversationId} agentId={boundAgent?.id ?? null} />
        </MobilePanelDrawer>
      )}
      {conversationId && (
        <MobilePanelDrawer
          open={todosPanelOpen}
          title="Tasks"
          onClose={() => setTodosPanelOpen(false)}
          testId="todos-panel-drawer"
        >
          <TodoPanel frameless />
        </MobilePanelDrawer>
      )}
      {conversationId && selectedFilePath !== null && (
        <div className="md:hidden">
          <FileViewer
            open
            conversationId={conversationId}
            path={selectedFilePath}
            onClose={closeFileViewer}
            onNavigateTo={openFileViewer}
            permissionLevel={permissionLevel}
            sort={filesPanelSort}
          />
        </div>
      )}
    </>
  );
}