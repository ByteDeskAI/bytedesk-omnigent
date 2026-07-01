import { type MouseEvent } from "react";
import { GitBranchIcon } from "lucide-react";
import { Link } from "@/lib/routing";
import { type Conversation } from "@/hooks/useConversations";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel } from "../../sidebarNav";

export function SidebarConversationRowLink({
  conversation,
  isActive,
  hasUnseenMessages,
  sessionStateKind,
  canEdit,
  onClick,
  onDoubleClickRename,
}: {
  conversation: Conversation;
  isActive: boolean;
  hasUnseenMessages: boolean;
  sessionStateKind: string | undefined;
  canEdit: boolean;
  onClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onDoubleClickRename: (e: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const label = conversationDisplayLabel(conversation);
  const gitBranch = conversation.git_branch ?? null;

  return (
    <Link
      to={`/c/${conversation.id}`}
      className={cn(
        "relative flex w-full flex-col gap-0.5 rounded-md px-4 py-2 text-left text-sm transition-colors duration-150 hover:bg-muted",
        sessionStateKind === "awaiting" ? "pr-44 md:pr-28" : "pr-28 md:pr-16",
        isActive &&
          "bg-muted font-semibold before:absolute before:top-1/2 before:left-0 before:h-5 before:w-0.5 before:-translate-y-1/2 before:rounded-full before:bg-primary before:content-['']",
      )}
      onClick={onClick}
      onDoubleClick={(e) => {
        if (!canEdit) return;
        e.preventDefault();
        onDoubleClickRename(e);
      }}
      title={conversation.title ?? conversation.id}
    >
      <div className="flex w-full items-center gap-1.5">
        <span className={cn("relative min-w-0 truncate", hasUnseenMessages && "font-semibold")}>
          {label}
          {hasUnseenMessages && <span className="sr-only"> (unread)</span>}
        </span>
      </div>
      {gitBranch !== null && (
        <span
          className="flex items-center gap-1 font-normal text-xs text-muted-foreground"
          title={gitBranch}
        >
          <GitBranchIcon className="size-3 shrink-0" />
          <span className="truncate">{gitBranch}</span>
        </span>
      )}
    </Link>
  );
}