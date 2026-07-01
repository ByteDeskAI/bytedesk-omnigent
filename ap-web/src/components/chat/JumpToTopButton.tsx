import { useCallback, useEffect, useState } from "react";
import { ArrowUpIcon, Loader2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import type { ConversationScroller } from "./ConversationScrollRefBridge";

/**
 * Hover-revealed "Jump to top" pill.
 */
export function JumpToTopButton({
  containerEl,
  scroller,
  hasMoreHistory,
}: {
  containerEl: HTMLElement | null;
  scroller: ConversationScroller | null;
  hasMoreHistory: boolean;
}) {
  const [atTop, setAtTop] = useState(true);
  const [hovering, setHovering] = useState(false);
  const [jumping, setJumping] = useState(false);

  const HOVER_BAND_PX = 140;

  useEffect(() => {
    if (!containerEl) return;
    const onMove = (e: MouseEvent) => {
      const next = e.clientY - containerEl.getBoundingClientRect().top < HOVER_BAND_PX;
      setHovering((prev) => (prev === next ? prev : next));
    };
    const onLeave = () => setHovering(false);
    containerEl.addEventListener("mousemove", onMove, { passive: true });
    containerEl.addEventListener("mouseleave", onLeave);
    return () => {
      containerEl.removeEventListener("mousemove", onMove);
      containerEl.removeEventListener("mouseleave", onLeave);
    };
  }, [containerEl]);

  const scrollEl = scroller?.el ?? null;
  useEffect(() => {
    if (!scrollEl) return;
    const onScroll = () => {
      const next = scrollEl.scrollTop <= 1;
      setAtTop((prev) => (prev === next ? prev : next));
    };
    onScroll();
    scrollEl.addEventListener("scroll", onScroll, { passive: true });
    return () => scrollEl.removeEventListener("scroll", onScroll);
  }, [scrollEl]);

  const canJump = hasMoreHistory || !atTop;
  const visible = jumping || (hovering && canJump);

  const jumpToTop = useCallback(async () => {
    if (!scroller) return;
    const { el, state, stopScroll } = scroller;
    const nextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    setJumping(true);
    try {
      stopScroll();
      state.isAtBottom = false;
      state.escapedFromLock = true;

      for (let i = 0; i < 1000 && useChatStore.getState().hasMoreHistory; i++) {
        await useChatStore.getState().loadMoreHistory();
        state.isAtBottom = false;
        state.escapedFromLock = true;
        await nextFrame();
      }
      // Pin to the very top, re-asserting across frames until it holds. The last
      // prepends keep growing scrollHeight after the store settles, and
      // HistoryAutoLoader's offset-preservation can bump scrollTop right after
      // we zero it. Force 0 each frame until it stays 0 for two consecutive
      // frames (or we hit the frame cap).
      for (let i = 0, stable = 0; i < 60 && stable < 2; i++) {
        if (el.scrollTop === 0) stable += 1;
        else {
          el.scrollTop = 0;
          stable = 0;
        }
        await nextFrame();
      }
    } finally {
      setJumping(false);
    }
  }, [scroller]);

  return (
    <div
      className={cn(
        "pointer-events-none absolute inset-x-0 top-[50px] z-40 flex justify-center transition-opacity duration-150",
        visible ? "opacity-100" : "opacity-0",
      )}
    >
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={jumping}
        onClick={() => void jumpToTop()}
        aria-label="Jump to the first message"
        tabIndex={visible ? 0 : -1}
        aria-hidden={!visible}
        className={cn(
          "h-7 gap-1.5 rounded-full px-3 text-xs shadow-sm",
          "bg-background hover:bg-background hover:brightness-95",
          "dark:bg-background dark:hover:bg-background dark:hover:brightness-125",
          visible ? "pointer-events-auto" : "pointer-events-none",
        )}
      >
        {jumping ? (
          <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
        ) : (
          <ArrowUpIcon className="size-3.5" aria-hidden />
        )}
        {jumping ? "Loading history…" : "Jump to top"}
      </Button>
    </div>
  );
}