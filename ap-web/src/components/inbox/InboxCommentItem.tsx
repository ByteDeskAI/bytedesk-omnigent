import { ArrowRightIcon } from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import type { CommentInboxItem } from "@/lib/inbox";
import { relativeTime } from "@/lib/relativeTime";
import { Link } from "@/lib/routing";
import { userColor, userInitials } from "@/lib/userBadge";
import { conversationDisplayLabel } from "@/shell/sidebarNav";

export function InboxCommentItem({ item }: { item: CommentInboxItem }) {
  const comment = item.comment;
  const author = comment.created_by ?? "You";
  const sessionTitle = conversationDisplayLabel(item.row);

  return (
    <div
      data-testid="inbox-comment"
      className="flex gap-3 rounded-xl border border-border bg-card p-4"
    >
      <Avatar size="sm" className="mt-0.5">
        <AvatarFallback
          className="font-medium text-white"
          style={{ backgroundColor: userColor(author) }}
        >
          {userInitials(author)}
        </AvatarFallback>
      </Avatar>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="min-w-0 truncate text-sm">
            <span className="font-medium">{author}</span>
            <span className="text-muted-foreground"> commented on </span>
            <span className="font-mono text-xs">{comment.path}</span>
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-2">
            <span className="text-xs text-muted-foreground">
              {relativeTime(comment.created_at * 1000)}
            </span>
            <Button asChild variant="ghost" size="sm" className="text-xs">
              <Link
                to={`/c/${item.row.id}?file=${encodeURIComponent(comment.path)}&comment=${encodeURIComponent(comment.id)}`}
              >
                Open file
                <ArrowRightIcon className="ml-1 size-3.5" />
              </Link>
            </Button>
          </span>
        </div>
        {comment.anchor_content && (
          <p className="truncate font-mono text-[11px] text-muted-foreground">
            {comment.anchor_content.trim()}
          </p>
        )}
        <p className="line-clamp-3 text-sm break-words whitespace-pre-wrap">{comment.body}</p>
        <span className="text-xs text-muted-foreground">{sessionTitle}</span>
      </div>
    </div>
  );
}