import { NewChatLandingAdvancedChip } from "./NewChatLandingAdvancedChip";
import { NewChatLandingHostChip } from "./NewChatLandingHostChip";
import { NewChatLandingSandboxRepoChip } from "./NewChatLandingSandboxRepoChip";
import { NewChatLandingWorkspaceSection } from "./NewChatLandingWorkspaceSection";
import { NewChatLandingWorktreeChip } from "./NewChatLandingWorktreeChip";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingFooterTray({ state }: { state: NewChatLandingState }) {
  const {
    sandboxSelected,
    selectedAgentDefaultHarness,
    supportsPermissionMode,
    supportsApprovalMode,
  } = state;

  return (
    <div className="relative z-0 -mt-9 flex w-full items-center rounded-b-2xl bg-tray/40 pt-8 pr-3 pb-2 pl-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <NewChatLandingHostChip state={state} />
        {sandboxSelected && <NewChatLandingSandboxRepoChip state={state} />}
        {!sandboxSelected && <NewChatLandingWorkspaceSection state={state} />}
        {!sandboxSelected && <NewChatLandingWorktreeChip state={state} />}
        {(selectedAgentDefaultHarness != null ||
          supportsPermissionMode ||
          supportsApprovalMode) && <NewChatLandingAdvancedChip state={state} />}
      </div>
    </div>
  );
}