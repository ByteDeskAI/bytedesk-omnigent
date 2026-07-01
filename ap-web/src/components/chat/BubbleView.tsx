import { memo } from "react";
import { Message, MessageContent } from "@/components/ai-elements/message";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { CompactionMarker } from "@/components/blocks/StatusBlocks";
import { bubblesEqual, type Bubble } from "@/lib/renderItems";
import { AssistantBubble } from "./AssistantBubble";
import { UserBubble } from "./UserBubble";

export const BubbleView = memo(
  function BubbleView({ bubble }: { bubble: Bubble }) {
    if (bubble.kind === "user") return <UserBubble bubble={bubble} />;
    if (bubble.kind === "compaction_loading") {
      return (
        <Message from="assistant" data-testid="compacting-indicator">
          <MessageContent>
            <Shimmer className="text-xs font-mono" duration={1.5}>
              Compacting conversation…
            </Shimmer>
          </MessageContent>
        </Message>
      );
    }
    if (bubble.kind === "compaction") return <CompactionMarker />;
    return <AssistantBubble bubble={bubble} />;
  },
  (prev, next) => bubblesEqual(prev.bubble, next.bubble),
);