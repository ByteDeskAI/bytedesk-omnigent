import {
  BotIcon,
  FileIcon,
  ListTodoIcon,
  TerminalIcon,
  WorkflowIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { type RightRailTab, TAB_BADGE_BASE } from "../../railTabs";
import { FileTabsStrip } from "../FileTabsStrip";

export function WorkspacePanelTabStrip({
  rightRailTab,
  onRightRailTabChange,
  selectedFilePath,
  showFilesPanel,
  changedCount,
  subagentsWorking,
  agentCount,
  showBlueprintTab,
  blueprintNodeCount,
  showShellsTab,
  terminalsLength,
  isClaudeNative,
  todosCompleted,
  todosTotal,
  openFiles,
  openFileViewer,
  onCloseFile,
}: {
  rightRailTab: RightRailTab;
  onRightRailTabChange: (next: RightRailTab) => void;
  selectedFilePath: string | null;
  showFilesPanel: boolean;
  changedCount: number;
  subagentsWorking: number;
  agentCount: number;
  showBlueprintTab: boolean;
  blueprintNodeCount: number;
  showShellsTab: boolean;
  terminalsLength: number;
  isClaudeNative: boolean;
  todosCompleted: number;
  todosTotal: number;
  openFiles: string[];
  openFileViewer: (path: string) => void;
  onCloseFile: (path: string) => void;
}) {
  return (
    <div className="shrink-0 flex items-center overflow-x-auto overflow-y-hidden border-b border-border px-2 py-1.5 [scrollbar-width:thin] @min-[500px]/rail:overflow-x-hidden [&::-webkit-scrollbar]:h-1 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
      <Tabs
        className="shrink-0"
        value={selectedFilePath !== null ? "__file__" : rightRailTab}
        onValueChange={(v) => onRightRailTabChange(v as RightRailTab)}
      >
        <TabsList variant="pill">
          {showFilesPanel && (
            <TabsTrigger
              value="files"
              className="h-[32px] gap-[6px] rounded-[8px] px-[12px] text-[13px] leading-5"
            >
              <FileIcon className="size-4" />
              Files
              {changedCount > 0 && (
                <span className={cn(TAB_BADGE_BASE, "ml-0.5 bg-muted text-muted-foreground")}>
                  {changedCount}
                </span>
              )}
            </TabsTrigger>
          )}
          <TabsTrigger
            value="subagents"
            className="h-[32px] gap-[6px] rounded-[8px] px-[12px] text-[13px] leading-5"
          >
            <BotIcon className="size-4" />
            Agents
            <span
              className={cn(
                TAB_BADGE_BASE,
                "ml-0.5",
                subagentsWorking > 0
                  ? "bg-success/15 text-success"
                  : "bg-muted text-muted-foreground",
              )}
            >
              {subagentsWorking > 0 ? `${subagentsWorking}/${agentCount}` : agentCount}
            </span>
          </TabsTrigger>
          {showBlueprintTab && (
            <TabsTrigger
              value="blueprint"
              className="h-[32px] gap-[6px] rounded-[8px] px-[12px] text-[13px] leading-5"
            >
              <WorkflowIcon className="size-4" />
              Blueprint
              <span className={cn(TAB_BADGE_BASE, "ml-0.5 bg-muted text-muted-foreground")}>
                {blueprintNodeCount}
              </span>
            </TabsTrigger>
          )}
          {showShellsTab && (
            <TabsTrigger
              value="terminals"
              className="h-[32px] gap-[6px] rounded-[8px] px-[12px] text-[13px] leading-5"
            >
              <TerminalIcon className="size-4" />
              Shells
              {terminalsLength > 0 && (
                <span className={cn(TAB_BADGE_BASE, "ml-0.5 bg-muted text-muted-foreground")}>
                  {terminalsLength}
                </span>
              )}
            </TabsTrigger>
          )}
          {isClaudeNative && todosTotal > 0 && (
            <TabsTrigger
              value="todos"
              className="h-[32px] gap-[6px] rounded-[8px] px-[12px] text-[13px] leading-5"
            >
              <ListTodoIcon className="size-4" />
              Tasks
              <span className={cn(TAB_BADGE_BASE, "ml-0.5 bg-muted text-muted-foreground")}>
                {todosCompleted}/{todosTotal}
              </span>
            </TabsTrigger>
          )}
        </TabsList>
      </Tabs>
      {openFiles.length > 0 && (
        <>
          <div
            aria-hidden
            className="mx-[4px] hidden h-[14px] w-px shrink-0 self-center bg-border-strong @min-[500px]/rail:block"
          />
          <div className="flex shrink-0 items-center [scrollbar-width:thin] @min-[500px]/rail:min-w-0 @min-[500px]/rail:flex-1 @min-[500px]/rail:overflow-x-auto @min-[500px]/rail:overflow-y-hidden [&::-webkit-scrollbar]:h-1 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
            <FileTabsStrip
              openFiles={openFiles}
              activeFilePath={selectedFilePath}
              onFileSelect={openFileViewer}
              onCloseFile={onCloseFile}
            />
          </div>
        </>
      )}
    </div>
  );
}