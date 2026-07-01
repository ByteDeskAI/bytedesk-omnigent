import { GitBranchIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { CHAT_COLUMN_WIDTH } from "./chat-utils";
import { ContextRing } from "./ContextRing";

/**
 * Status-line tray tucked behind the composer card.
 */
export function ComposerStatusLine() {
  const conversationId = useChatStore((s) => s.conversationId);
  const contextWindow = useChatStore((s) => s.contextWindow);
  const tokensUsed = useChatStore((s) => s.tokensUsed);
  const gitBranch = useChatStore((s) => s.gitBranch);

  const showBranch = !!conversationId && !!gitBranch;
  const showRing =
    !!conversationId && contextWindow != null && contextWindow > 0 && tokensUsed != null;
  if (!showBranch && !showRing) return null;

  return (
    <div
      data-testid="composer-status-line"
      className={cn(
        "mx-auto -mt-4 flex w-full items-center gap-3 rounded-b-2xl bg-tray/40 px-4 pb-1.5 pt-5.5",
        CHAT_COLUMN_WIDTH,
      )}
    >
      <span className="flex min-w-0 flex-1 items-center gap-1.5 text-xs text-muted-foreground">
        {showBranch && (
          <>
            <GitBranchIcon className="size-3.5 shrink-0" />
            <span data-testid="composer-git-branch" className="min-w-0 truncate" title={gitBranch}>
              {gitBranch}
            </span>
          </>
        )}
      </span>
      <div className="flex shrink-0 items-center gap-3">
        {showRing && <ContextRing contextWindow={contextWindow} tokensUsed={tokensUsed} />}
      </div>
    </div>
  );
}