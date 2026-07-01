import { ChevronDownIcon, SettingsIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { NewChatApprovalModeOptions } from "./NewChatApprovalModeOptions";
import { NewChatBrainHarnessOptions } from "./NewChatBrainHarnessOptions";
import { NewChatPermissionModeOptions } from "./NewChatPermissionModeOptions";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingAdvancedChip({ state }: { state: NewChatLandingState }) {
  const {
    selectedAgentDefaultHarness,
    supportsPermissionMode,
    supportsApprovalMode,
    pickedHarness,
    setPickedHarness,
    harnessWarningHost,
    permissionMode,
    setPermissionMode,
    approvalMode,
    setApprovalMode,
  } = state;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
          data-testid="new-chat-landing-advanced-chip"
        >
          <SettingsIcon className="size-4 shrink-0" />
          <span className="truncate">Advanced</span>
          <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="max-h-[var(--radix-dropdown-menu-content-available-height)] w-64 max-w-[calc(100vw-2rem)] overflow-y-auto p-1"
      >
        {selectedAgentDefaultHarness != null && (
          <NewChatBrainHarnessOptions
            value={pickedHarness ?? selectedAgentDefaultHarness}
            onValueChange={(h) =>
              setPickedHarness(h === selectedAgentDefaultHarness ? null : h)
            }
            host={harnessWarningHost}
          />
        )}
        {supportsPermissionMode && (
          <>
            {selectedAgentDefaultHarness != null && <DropdownMenuSeparator />}
            <div className="px-2 pt-1.5 pb-0.5 text-[11px] font-medium text-muted-foreground">
              Permission mode
            </div>
            <NewChatPermissionModeOptions value={permissionMode} onValueChange={setPermissionMode} />
          </>
        )}
        {supportsApprovalMode && (
          <>
            {(selectedAgentDefaultHarness != null || supportsPermissionMode) && (
              <DropdownMenuSeparator />
            )}
            <div className="px-2 pt-1.5 pb-0.5 text-[11px] font-medium text-muted-foreground">
              Approval mode
            </div>
            <NewChatApprovalModeOptions value={approvalMode} onValueChange={setApprovalMode} />
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}