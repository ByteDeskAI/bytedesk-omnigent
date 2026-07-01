import { Loader2Icon } from "lucide-react";
import {
  InboxApprovalItem,
  InboxCommentItem,
  InboxEmptyState,
  InboxLoadErrorBanner,
  InboxLoadingIndicator,
} from "@/components/inbox";
import type { CommentInboxItem, InboxItem } from "@/lib/inbox";

type RespondedMap = Record<
  string,
  { action: "accept" | "decline"; content?: Record<string, unknown> }
>;

export interface InboxPageShellProps {
  items: InboxItem[];
  commentItems: CommentInboxItem[];
  assembling: boolean;
  failedSessionCount: number;
  responded: RespondedMap;
  expandedOverrides: Record<string, boolean>;
  onRetryFailed: () => void;
  onToggleExpanded: (elicitationId: string, expanded: boolean) => void;
  onSubmit: (
    item: InboxItem,
  ) => (elicitationId: string, action: "accept" | "decline", content?: Record<string, unknown>) => void;
}

export function InboxPageShell({
  items,
  commentItems,
  assembling,
  failedSessionCount,
  responded,
  expandedOverrides,
  onRetryFailed,
  onToggleExpanded,
  onSubmit,
}: InboxPageShellProps) {
  return (
    <div className="mx-auto w-full max-w-3xl overflow-y-auto px-6 py-8 pt-14">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Inbox</h1>
        {(items.length > 0 || commentItems.length > 0) && (
          <span className="text-sm text-muted-foreground">
            {[
              items.length > 0 && (items.length === 1 ? "1 approval" : `${items.length} approvals`),
              commentItems.length > 0 &&
                (commentItems.length === 1 ? "1 comment" : `${commentItems.length} comments`),
            ]
              .filter(Boolean)
              .join(" · ")}{" "}
            waiting
          </span>
        )}
      </div>

      {failedSessionCount > 0 && (
        <InboxLoadErrorBanner failedSessionCount={failedSessionCount} onRetry={onRetryFailed} />
      )}

      {assembling && items.length === 0 && commentItems.length === 0 && (
        <InboxLoadingIndicator label="Loading inbox…" />
      )}

      {!assembling &&
        failedSessionCount === 0 &&
        items.length === 0 &&
        commentItems.length === 0 && <InboxEmptyState />}

      <div className="mc-stagger-children flex flex-col gap-4">
        {items.map((item, index) => {
          const elicitationId = item.elicitation.elicitationId;
          const verdict = responded[elicitationId];
          const expanded = expandedOverrides[elicitationId] ?? index === 0;
          return (
            <InboxApprovalItem
              key={elicitationId}
              item={item}
              expanded={expanded}
              verdict={verdict}
              onToggleExpanded={() => onToggleExpanded(elicitationId, expanded)}
              onSubmit={onSubmit(item)}
            />
          );
        })}
        {commentItems.map((item) => (
          <InboxCommentItem key={item.comment.id} item={item} />
        ))}
        {assembling && (items.length > 0 || commentItems.length > 0) && (
          <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
            <Loader2Icon className="size-3.5 animate-spin" />
            Checking remaining sessions…
          </div>
        )}
      </div>
    </div>
  );
}