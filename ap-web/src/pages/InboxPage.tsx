/**
 * Inbox page (``/inbox``) — every approval prompt waiting on the user,
 * across all of their sessions, rendered as actionable cards.
 */

import { useEffect, useState } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { useCommentInbox } from "@/hooks/useCommentInbox";
import { useConversations } from "@/hooks/useConversations";
import { collectInboxItems, type InboxItem, type InboxSource } from "@/lib/inbox";
import { approve, getSession } from "@/lib/sessionsApi";
import { InboxPageShell } from "./organisms/InboxPageShell";

type RespondedMap = Record<
  string,
  { action: "accept" | "decline"; content?: Record<string, unknown> }
>;

export function InboxPage() {
  const queryClient = useQueryClient();
  const conversationsQuery = useConversations("", false, { reconcileWhileConnected: true });
  const [responded, setResponded] = useState<RespondedMap>({});
  const [expandedOverrides, setExpandedOverrides] = useState<Record<string, boolean>>({});

  const { hasNextPage, isFetchingNextPage, fetchNextPage } = conversationsQuery;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const allRows = (conversationsQuery.data?.pages ?? []).flatMap((page) => page.data);
  const rows = allRows.filter((c) => !c.archived && (c.pending_elicitations_count ?? 0) > 0);
  const commentInbox = useCommentInbox(allRows);

  const snapshotQueries = useQueries({
    queries: rows.map((row) => ({
      queryKey: ["inbox-elicitations", row.id, row.pending_elicitations_count],
      queryFn: () => getSession(row.id),
      retry: 1,
    })),
  });

  const sources: InboxSource[] = [];
  rows.forEach((row, i) => {
    const snapshot = snapshotQueries[i]?.data;
    if (snapshot) sources.push({ row, pendingElicitations: snapshot.pendingElicitations ?? [] });
  });
  const items = collectInboxItems(sources);

  const assembling =
    conversationsQuery.isLoading ||
    hasNextPage ||
    isFetchingNextPage ||
    snapshotQueries.some((q) => q.isLoading) ||
    commentInbox.isLoading;
  const failedSnapshots = snapshotQueries.filter((q) => q.isError);
  const failedSessionCount = failedSnapshots.length + commentInbox.failedCount;

  const makeSubmit = (item: InboxItem) => {
    return (elicitationId: string, action: "accept" | "decline", content?: Record<string, unknown>) => {
      setResponded((prev) => ({
        ...prev,
        [elicitationId]: content === undefined ? { action } : { action, content },
      }));
      void approve(
        item.resolveSessionId,
        elicitationId,
        content === undefined ? { action } : { action, content },
      ).then(
        () => {
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
        },
        () => {
          setResponded((prev) => {
            const next = { ...prev };
            delete next[elicitationId];
            return next;
          });
        },
      );
    };
  };

  return (
    <InboxPageShell
      items={items}
      commentItems={commentInbox.items}
      assembling={assembling}
      failedSessionCount={failedSessionCount}
      responded={responded}
      expandedOverrides={expandedOverrides}
      onRetryFailed={() => {
        failedSnapshots.forEach((q) => void q.refetch());
        commentInbox.retryFailed();
      }}
      onToggleExpanded={(elicitationId, expanded) =>
        setExpandedOverrides((prev) => ({ ...prev, [elicitationId]: !expanded }))
      }
      onSubmit={makeSubmit}
    />
  );
}