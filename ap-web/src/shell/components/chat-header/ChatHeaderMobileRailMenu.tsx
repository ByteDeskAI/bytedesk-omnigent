import {
  BotIcon,
  FileIcon,
  ListIcon,
  ListTodoIcon,
  PanelRightIcon,
  TerminalIcon,
  WorkflowIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { TAB_BADGE_BASE } from "../../railTabs";

export interface MobileSessionMenuProps {
  fileViewerOpen: boolean;
  panelOpen: boolean;
  terminalFirst: boolean;
  executionLogsOpen: boolean;
  filesPanelOpen: boolean;
  subagentsPanelOpen: boolean;
  todosPanelOpen: boolean;
  blueprintPanelOpen: boolean;
  hideTerminalsTab: boolean;
  terminalsLength: number;
  isClaudeNative: boolean;
  todosCompleted: number;
  todosTotal: number;
  debugMode: boolean;
  changedCount: number;
  subagentsWorking: number;
  showBlueprintTab: boolean;
  blueprintNodeCount: number;
  agentCount: number;
  onOpenFiles: () => void;
  onOpenFirstTerminal: () => void;
  onOpenSubagents: () => void;
  onOpenBlueprint: () => void;
  onOpenTodos: () => void;
  onOpenMainExecutionLog: () => void;
}

export function ChatHeaderMobileRailMenu({
  conversationId,
  showFilesPanel,
  hasRailContent,
  mobileMenu,
}: {
  conversationId: string | undefined;
  showFilesPanel: boolean;
  hasRailContent: boolean;
  mobileMenu: MobileSessionMenuProps;
}) {
  const showFab =
    conversationId &&
    !mobileMenu.fileViewerOpen &&
    (!mobileMenu.panelOpen || mobileMenu.terminalFirst) &&
    !mobileMenu.executionLogsOpen &&
    !mobileMenu.filesPanelOpen &&
    !mobileMenu.subagentsPanelOpen &&
    !mobileMenu.blueprintPanelOpen &&
    !mobileMenu.todosPanelOpen &&
    (hasRailContent || mobileMenu.debugMode);

  if (!showFab) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Open session menu"
          className="text-muted-foreground hover:text-foreground md:hidden"
        >
          <PanelRightIcon className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-44">
        {showFilesPanel && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenFiles}
            className="gap-2.5 px-2.5 py-2 text-base"
          >
            <FileIcon className="size-4" />
            Files
            {mobileMenu.changedCount > 0 && (
              <span className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}>
                {mobileMenu.changedCount}
              </span>
            )}
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          onSelect={mobileMenu.onOpenSubagents}
          className="gap-2.5 px-2.5 py-2 text-base"
        >
          <BotIcon className="size-4" />
          Agents
          <span
            className={cn(
              TAB_BADGE_BASE,
              "ml-auto",
              mobileMenu.subagentsWorking > 0
                ? "bg-success/15 text-success"
                : "bg-muted text-muted-foreground",
            )}
          >
            {mobileMenu.subagentsWorking > 0
              ? `${mobileMenu.subagentsWorking}/${mobileMenu.agentCount}`
              : mobileMenu.agentCount}
          </span>
        </DropdownMenuItem>
        {mobileMenu.showBlueprintTab && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenBlueprint}
            className="gap-2.5 px-2.5 py-2 text-base"
          >
            <WorkflowIcon className="size-4" />
            Blueprint
            <span className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}>
              {mobileMenu.blueprintNodeCount}
            </span>
          </DropdownMenuItem>
        )}
        {!mobileMenu.hideTerminalsTab && mobileMenu.terminalsLength > 0 && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenFirstTerminal}
            className="gap-2.5 px-2.5 py-2 text-base"
          >
            <TerminalIcon className="size-4" />
            Shells
            <span className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}>
              {mobileMenu.terminalsLength}
            </span>
          </DropdownMenuItem>
        )}
        {mobileMenu.isClaudeNative && mobileMenu.todosTotal > 0 && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenTodos}
            className="gap-2.5 px-2.5 py-2 text-base"
          >
            <ListTodoIcon className="size-4" />
            Tasks
            <span className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}>
              {mobileMenu.todosCompleted}/{mobileMenu.todosTotal}
            </span>
          </DropdownMenuItem>
        )}
        {mobileMenu.debugMode && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenMainExecutionLog}
            className="gap-2.5 px-2.5 py-2 text-base"
          >
            <ListIcon className="size-4" />
            Logs
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}