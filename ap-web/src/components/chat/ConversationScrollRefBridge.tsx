import { useEffect } from "react";
import { useStickToBottomContext } from "use-stick-to-bottom";

/**
 * The conversation's scroll container plus the minimal StickToBottom controls
 * the JumpToTopButton needs to override the library's bottom-lock.
 */
export type ConversationScroller = {
  el: HTMLElement;
  state: { isAtBottom: boolean; escapedFromLock: boolean };
  stopScroll: () => void;
};

/**
 * Lifts the StickToBottom scroll container out of the context so a sibling
 * rendered outside `<Conversation>` can still read and drive it.
 */
export function ConversationScrollRefBridge({
  onScroller,
}: {
  onScroller: (s: ConversationScroller | null) => void;
}) {
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    scrollRef: React.RefObject<HTMLElement>;
    state: ConversationScroller["state"];
    stopScroll: () => void;
  };
  useEffect(() => {
    const el = ctx.scrollRef?.current ?? null;
    onScroller(el ? { el, state: ctx.state, stopScroll: ctx.stopScroll } : null);
    return () => onScroller(null);
  }, [ctx.scrollRef, ctx.state, ctx.stopScroll, onScroller]);
  return null;
}