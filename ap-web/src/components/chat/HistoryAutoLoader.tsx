import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { useStickToBottomContext } from "use-stick-to-bottom";
import { useChatStore } from "@/store/chatStore";

/**
 * Headless older-history loader. Pages older session items in two ways
 * with no visible control.
 */
export function HistoryAutoLoader({
  hasMoreHistory,
  loadingMoreHistory,
  sendScrollNonce = 0,
}: {
  hasMoreHistory: boolean;
  loadingMoreHistory: boolean;
  sendScrollNonce?: number;
}) {
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    scrollRef: React.RefObject<HTMLElement>;
  };

  const prevScrollHeightRef = useRef<number | null>(null);
  const lastSendScrollAtRef = useRef(0);
  useEffect(() => {
    if (sendScrollNonce === 0) return;
    lastSendScrollAtRef.current =
      typeof performance !== "undefined" ? performance.now() : Date.now();
  }, [sendScrollNonce]);
  const isSendScrollSettling = useCallback(() => {
    if (lastSendScrollAtRef.current === 0) return false;
    const now = typeof performance !== "undefined" ? performance.now() : Date.now();
    return now - lastSendScrollAtRef.current < 300;
  }, []);
  const loadOlderPreservingOffset = useCallback(() => {
    if (!hasMoreHistory || loadingMoreHistory) return;
    if (isSendScrollSettling()) return;
    const el = ctx.scrollRef?.current;
    if (el) prevScrollHeightRef.current = el.scrollHeight;
    void useChatStore.getState().loadMoreHistory();
  }, [ctx.scrollRef, hasMoreHistory, isSendScrollSettling, loadingMoreHistory]);

  useLayoutEffect(() => {
    const el = ctx.scrollRef?.current;
    if (!el || prevScrollHeightRef.current === null || loadingMoreHistory) return;
    const delta = el.scrollHeight - prevScrollHeightRef.current;
    if (delta > 0) el.scrollTop += delta;
    prevScrollHeightRef.current = null;
  });

  useEffect(() => {
    const el = ctx.scrollRef?.current;
    if (!el) return;
    const handleScroll = () => {
      if (el.scrollTop < 300 && hasMoreHistory && !loadingMoreHistory) {
        loadOlderPreservingOffset();
      }
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, [ctx.scrollRef, hasMoreHistory, loadingMoreHistory, loadOlderPreservingOffset]);

  const maybeFillViewport = useCallback(() => {
    const el = ctx.scrollRef?.current;
    if (!el || !hasMoreHistory || loadingMoreHistory) return;
    if (isSendScrollSettling()) return;
    if (el.scrollHeight <= el.clientHeight) {
      void useChatStore.getState().loadMoreHistory();
    }
  }, [ctx.scrollRef, hasMoreHistory, isSendScrollSettling, loadingMoreHistory]);

  useEffect(() => {
    maybeFillViewport();
  }, [maybeFillViewport]);

  useEffect(() => {
    const el = ctx.scrollRef?.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => maybeFillViewport());
    observer.observe(el);
    return () => observer.disconnect();
  }, [ctx.scrollRef, maybeFillViewport]);

  return null;
}