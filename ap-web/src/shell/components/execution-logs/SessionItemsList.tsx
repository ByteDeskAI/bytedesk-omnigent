import { useEffect, useRef } from "react";
import { useChatStore } from "@/store/chatStore";
import { useSessionItems } from "@/hooks/useSessionItems";
import { executionLogItemKey } from "./executionLogsUtils";
import { SessionItemEntry } from "./SessionItemEntry";

const ITEMS_POLL_MS = 3_000;

function useFocusedSessionActive(): boolean {
  const status = useChatStore((s) => s.sessionStatus);
  return status === "running" || status === "waiting";
}

export function SessionItemsList({ sessionId }: { sessionId: string }) {
  const sessionActive = useFocusedSessionActive();
  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useSessionItems(sessionId, sessionActive ? ITEMS_POLL_MS : null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const scrollRootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    const root = scrollRootRef.current;
    if (!sentinel || !root || !hasNextPage || isFetchingNextPage) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          fetchNextPage();
        }
      },
      { root, rootMargin: "200px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  if (isLoading) {
    return <div className="text-muted-foreground text-xs">Loading…</div>;
  }
  if (error) {
    return <div className="text-destructive text-xs">Failed to load items: {error.message}</div>;
  }
  if (items.length === 0) {
    return <div className="text-muted-foreground text-xs">No items</div>;
  }
  return (
    <div
      ref={scrollRootRef}
      className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto pr-1 font-mono text-xs"
    >
      {items.map((item, idx) => (
        <SessionItemEntry key={executionLogItemKey(item, idx)} item={item} index={idx + 1} />
      ))}
      {hasNextPage && (
        <div ref={sentinelRef} className="py-2 text-center text-muted-foreground text-xs">
          {isFetchingNextPage ? "Loading more…" : ""}
        </div>
      )}
    </div>
  );
}