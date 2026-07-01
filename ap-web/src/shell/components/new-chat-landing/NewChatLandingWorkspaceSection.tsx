import { ChevronDownIcon, FolderIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { WorkspacePicker, isNavigablePath } from "../../WorkspacePicker";
import { normalizeWorkspacePath } from "./newChatLandingUtils";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingWorkspaceSection({ state }: { state: NewChatLandingState }) {
  const {
    workspacePopoverOpen,
    setWorkspacePopoverOpen,
    workspaceTrimmed,
    workspaceLabel,
    selectedHostId,
    setWorkspace,
    occupancyByDir,
    branchName,
  } = state;

  return (
    <Popover open={workspacePopoverOpen} onOpenChange={setWorkspacePopoverOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
          data-testid="new-chat-landing-workspace-chip"
        >
          <FolderIcon className="size-4 shrink-0" />
          <span className={`max-w-40 truncate ${workspaceTrimmed !== "" ? "text-foreground" : ""}`}>
            {workspaceLabel}
          </span>
          <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(420px,calc(100vw-2rem))] p-0">
        {selectedHostId ? (
          <WorkspacePicker
            hostId={selectedHostId}
            initialPath={isNavigablePath(workspaceTrimmed) ? workspaceTrimmed : undefined}
            onNavigate={setWorkspace}
            occupancyForPath={
              branchName.trim() === ""
                ? (abs) => occupancyByDir.get(normalizeWorkspacePath(abs) ?? "") ?? 0
                : undefined
            }
          />
        ) : (
          <p className="p-3 text-xs text-muted-foreground">Select a host first.</p>
        )}
      </PopoverContent>
    </Popover>
  );
}