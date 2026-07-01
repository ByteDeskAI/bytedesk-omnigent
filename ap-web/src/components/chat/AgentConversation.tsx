import { type ReactNode, useMemo, useRef } from "react";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { HistoryAutoLoader } from "./HistoryAutoLoader";
import {
  buildPendingBubbles,
  computeShowsWorking,
  mergePendingBubbles,
  shouldShowWorkingIndicator,
} from "./chat-utils";
import {
  type Bubble,
  type BubbleCache,
  buildBubbles,
  createBubbleCache,
} from "@/lib/renderItems";
import { getCurrentAuthorId } from "@/lib/identity";
import { useChatStore } from "@/store/chatStore";
import { cn } from "@/lib/utils";
import { ConversationBubbleList } from "./ConversationBubbleList";

interface AgentConversationProps {
  /** Replaces the default "Ask me to find and install a skill." empty state. */
  emptyState?: ReactNode;
  className?: string;
}

/**
 * Self-contained chat scroll surface for EMBEDDED chat (e.g. the Skills
 * page). Subscribes to the shared chatStore and mirrors ChatPage's bubble
 * build + working-indicator derivation, but without the full page's
 * terminal/reconnect/permission machinery.
 */
export function AgentConversation({ emptyState, className }: AgentConversationProps) {
  const blocks = useChatStore((s) => s.blocks);
  const pendingUserMessages = useChatStore((s) => s.pendingUserMessages);
  const activeResponse = useChatStore((s) => s.activeResponse);
  const interruptedResponseIds = useChatStore((s) => s.interruptedResponseIds);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const hasMoreHistory = useChatStore((s) => s.hasMoreHistory);
  const loadingMoreHistory = useChatStore((s) => s.loadingMoreHistory);

  const bubbleCacheRef = useRef<BubbleCache>(createBubbleCache());
  const bubbles = useMemo<Bubble[]>(() => {
    const committed = buildBubbles(
      blocks,
      activeResponse,
      bubbleCacheRef.current,
      interruptedResponseIds,
    );
    if (pendingUserMessages.length === 0) return committed;
    return mergePendingBubbles(
      committed,
      buildPendingBubbles(pendingUserMessages, getCurrentAuthorId()),
    );
  }, [blocks, activeResponse, interruptedResponseIds, pendingUserMessages]);

  const showWorkingIndicator = shouldShowWorkingIndicator(
    computeShowsWorking(sessionStatus, { hasPendingElicitation: false, runnerOnline: undefined }),
    bubbles,
  );

  return (
    <Conversation className={cn("flex-1", className)}>
      <ConversationContent className="mx-auto w-full max-w-3xl gap-4">
        <HistoryAutoLoader
          hasMoreHistory={hasMoreHistory}
          loadingMoreHistory={loadingMoreHistory}
        />
        <ConversationBubbleList
          bubbles={bubbles}
          showWorkingIndicator={showWorkingIndicator}
          emptyState={
            emptyState ?? (
              <ConversationEmptyState>
                Ask me to find and install a skill.
              </ConversationEmptyState>
            )
          }
        />
      </ConversationContent>
      <ConversationScrollButton />
    </Conversation>
  );
}
