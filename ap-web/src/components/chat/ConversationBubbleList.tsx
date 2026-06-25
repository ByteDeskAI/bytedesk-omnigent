import type { ReactNode } from "react";
import { Message, MessageContent } from "@/components/ai-elements/message";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { AgentMascotEyes } from "@/components/AgentMascotEyes";
import { BubbleView, RunnerStartingIndicator, bubbleKey } from "@/pages/ChatPage";
import type { Bubble } from "@/lib/renderItems";

interface ConversationBubbleListProps {
  bubbles: Bubble[];
  showWorkingIndicator: boolean;
  /** Rendered when the list is empty and nothing is working. */
  emptyState: ReactNode;
  /** Cold-launch spinner instead of the empty state (terminal spin-up / sandbox launch). */
  launching?: boolean;
}

/**
 * The empty / bubble-list / Working… surface lifted verbatim out of
 * ChatPage's MainAgentSurface so it can be reused by embedded chat
 * (AgentConversation) without dragging the whole page in.
 */
export function ConversationBubbleList({
  bubbles,
  showWorkingIndicator,
  emptyState,
  launching = false,
}: ConversationBubbleListProps) {
  if (bubbles.length === 0 && !showWorkingIndicator) {
    return launching ? <RunnerStartingIndicator variant="hero" /> : <>{emptyState}</>;
  }
  return (
    <>
      {bubbles.map((bubble) => (
        <BubbleView key={bubbleKey(bubble)} bubble={bubble} />
      ))}
      {/* Working… shimmer between send and first rendered block.
          Suppressed when the last bubble is a compaction spinner —
          that bubble already owns the "in-progress" slot. aria-hidden:
          the pinned pill owns the single aria-live region (see WorkingStatusPin). */}
      {showWorkingIndicator && (
        <Message from="assistant" data-testid="working-indicator" aria-hidden="true">
          <MessageContent>
            {/* py-0.5 = headroom for the bob: MessageContent is overflow-hidden
                and would clip the mascot at the top of the bounce. */}
            <div className="flex items-center gap-1.5 py-0.5">
              <AgentMascotEyes decorative className="otto-working h-4 w-auto shrink-0" />
              <Shimmer className="text-xs font-mono" duration={1.5}>
                Working…
              </Shimmer>
            </div>
          </MessageContent>
        </Message>
      )}
      {/* Terminal-first spin-up cue beneath the just-sent first
          message: the prompt bubble renders immediately (no
          runner-online send gate), but `showWorkingIndicator` stays
          suppressed while the runner is offline, so without this the
          user's message sits with no sign anything is happening.
          Self-gates to null off the spin-up window; rendered only
          when not already showing Working… so the two never stack. */}
      {!showWorkingIndicator && <RunnerStartingIndicator variant="row" />}
    </>
  );
}
