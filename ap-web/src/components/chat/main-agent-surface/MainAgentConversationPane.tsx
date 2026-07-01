import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import type { Bubble } from "@/lib/renderItems";
import { cn } from "@/lib/utils";
import { ConversationBubbleList } from "../ConversationBubbleList";
import { HistoryAutoLoader } from "../HistoryAutoLoader";
import { JumpToTopButton } from "../JumpToTopButton";
import { ScrollToBottomOnSend } from "../ScrollToBottomOnSend";
import { UserMessageNavConnected } from "../UserMessageNavConnected";
import { WorkingStatusPin } from "../WorkingStatusPin";
import {
  ConversationScrollRefBridge,
  type ConversationScroller,
} from "../ConversationScrollRefBridge";
import { CHAT_COLUMN_WIDTH } from "../chat-utils";

export function MainAgentConversationPane({
  setConversationEl,
  containerEl,
  scroller,
  onScroller,
  sendScrollNonce,
  bubbles,
  showWorkingIndicator,
  launching,
  agentsError,
  hasMoreHistory,
  loadingMoreHistory,
  subAgentLabel,
  nav,
  userMessageIds,
}: {
  setConversationEl: (el: HTMLDivElement | null) => void;
  containerEl: HTMLElement | null;
  scroller: ConversationScroller | null;
  onScroller: (scroller: ConversationScroller | null) => void;
  sendScrollNonce: number;
  bubbles: Bubble[];
  showWorkingIndicator: boolean;
  launching: boolean;
  agentsError: unknown;
  hasMoreHistory: boolean;
  loadingMoreHistory: boolean;
  subAgentLabel: string | null;
  nav: {
    goPrev: () => void;
    goNext: () => void;
    canPrev: boolean;
    canNext: boolean;
  };
  userMessageIds: string[];
}) {
  return (
    <div ref={setConversationEl} className="relative flex min-h-0 flex-1 overflow-hidden">
      <Conversation className="chat-scroll-fade flex-1">
        <ConversationContent className={cn("mx-auto w-full gap-4 pt-20 pb-6", CHAT_COLUMN_WIDTH)}>
          <ScrollToBottomOnSend nonce={sendScrollNonce} />
          <ConversationScrollRefBridge onScroller={onScroller} />
          <HistoryAutoLoader
            hasMoreHistory={hasMoreHistory}
            loadingMoreHistory={loadingMoreHistory}
            sendScrollNonce={sendScrollNonce}
          />
          <ConversationBubbleList
            bubbles={bubbles}
            showWorkingIndicator={showWorkingIndicator}
            launching={launching}
            emptyState={
              <ConversationEmptyState>
                <div className="space-y-1.5">
                  <h3 className="text-2xl font-medium tracking-[-0.02em]">What should we work on?</h3>
                  <p className="text-muted-foreground text-base">
                    {agentsError
                      ? `Failed to load agents: ${agentsError instanceof Error ? agentsError.message : String(agentsError)}`
                      : "Send a message to get started."}
                  </p>
                </div>
              </ConversationEmptyState>
            }
          />
        </ConversationContent>
        <ConversationScrollButton />
        <WorkingStatusPin show={showWorkingIndicator} suppress={subAgentLabel != null} />
        <UserMessageNavConnected
          goPrev={nav.goPrev}
          goNext={nav.goNext}
          canPrev={nav.canPrev}
          canNext={nav.canNext}
          hidden={userMessageIds.length === 0}
        />
      </Conversation>
      <JumpToTopButton
        containerEl={containerEl}
        scroller={scroller}
        hasMoreHistory={hasMoreHistory}
      />
    </div>
  );
}