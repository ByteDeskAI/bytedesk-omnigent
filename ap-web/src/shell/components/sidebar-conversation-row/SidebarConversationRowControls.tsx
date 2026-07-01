import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  CircleStopIcon,
  MoreHorizontalIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  ShareIcon,
  Trash2Icon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { type Conversation } from "@/hooks/useConversations";
import { cn } from "@/lib/utils";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { TIME_MARKER_SLOT_CLASS } from "../sidebarConversationConstants";
import type { getSessionState } from "@/hooks/useSessionState";

export function SidebarConversationRowControls({
  conversation,
  isPinned,
  sessionState,
  isOwner,
  canManage,
  canEdit,
  canStop,
  isArchived,
  onTogglePinned,
  onArchive,
  onShare,
  onRename,
  onStop,
  onDelete,
}: {
  conversation: Conversation;
  isPinned: boolean;
  sessionState: ReturnType<typeof getSessionState>;
  isOwner: boolean;
  canManage: boolean;
  canEdit: boolean;
  canStop: boolean;
  isArchived: boolean;
  onTogglePinned: (conversationId: string) => void;
  onArchive: () => void;
  onShare: () => void;
  onRename: () => void;
  onStop: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      {sessionState !== null ? (
        <span className={TIME_MARKER_SLOT_CLASS}>
          <SessionStateBadge state={sessionState} />
        </span>
      ) : (
        <span
          className={cn(TIME_MARKER_SLOT_CLASS, "text-xs tabular-nums text-muted-foreground")}
          aria-label={absoluteTime(conversation.updated_at * 1000)}
          title={absoluteTime(conversation.updated_at * 1000)}
        >
          {relativeTime(conversation.updated_at * 1000)}
        </span>
      )}
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={isPinned ? "Unpin conversation" : "Pin conversation"}
        data-testid="quick-pin-conversation"
        className={cn(
          "-translate-y-1/2 absolute top-1/2 right-9 transition-opacity",
          "md:opacity-0 md:group-hover:opacity-100",
          "md:group-has-[:focus-visible]:opacity-100 md:group-has-[[aria-expanded=true]]:opacity-100",
        )}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onTogglePinned(conversation.id);
        }}
      >
        {isPinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Conversation actions"
            data-testid="conversation-actions"
            className={cn(
              "-translate-y-1/2 absolute top-1/2 right-1 transition-opacity",
              "md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100",
              "md:aria-expanded:opacity-100",
            )}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
            }}
          >
            <MoreHorizontalIcon className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-36">
          {isOwner ? (
            <DropdownMenuItem data-testid="archive-conversation" onSelect={onArchive}>
              {isArchived ? (
                <ArchiveRestoreIcon className="size-3.5" />
              ) : (
                <ArchiveIcon className="size-3.5" />
              )}
              {isArchived ? "Unarchive" : "Archive"}
            </DropdownMenuItem>
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                <div>
                  <DropdownMenuItem data-testid="archive-conversation" disabled>
                    {isArchived ? (
                      <ArchiveRestoreIcon className="size-3.5" />
                    ) : (
                      <ArchiveIcon className="size-3.5" />
                    )}
                    {isArchived ? "Unarchive" : "Archive"}
                  </DropdownMenuItem>
                </div>
              </TooltipTrigger>
              <TooltipContent side="left">
                Only the session owner can {isArchived ? "unarchive" : "archive"} this session
              </TooltipContent>
            </Tooltip>
          )}
          {canManage ? (
            <DropdownMenuItem data-testid="share-conversation" onSelect={onShare}>
              <ShareIcon className="size-3.5" />
              Share
            </DropdownMenuItem>
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                <div>
                  <DropdownMenuItem data-testid="share-conversation" disabled>
                    <ShareIcon className="size-3.5" />
                    Share
                  </DropdownMenuItem>
                </div>
              </TooltipTrigger>
              <TooltipContent side="left">
                You need manage permissions to share this session
              </TooltipContent>
            </Tooltip>
          )}
          {canEdit ? (
            <DropdownMenuItem data-testid="rename-conversation" onSelect={onRename}>
              <PencilIcon className="size-3.5" />
              Rename
            </DropdownMenuItem>
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                <div>
                  <DropdownMenuItem data-testid="rename-conversation" disabled>
                    <PencilIcon className="size-3.5" />
                    Rename
                  </DropdownMenuItem>
                </div>
              </TooltipTrigger>
              <TooltipContent side="left">
                You need edit permissions to rename this session
              </TooltipContent>
            </Tooltip>
          )}
          {canStop &&
            (isOwner ? (
              <DropdownMenuItem
                data-testid="stop-conversation"
                variant="destructive"
                onSelect={onStop}
              >
                <CircleStopIcon className="size-3.5" />
                Stop session
              </DropdownMenuItem>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <div>
                    <DropdownMenuItem data-testid="stop-conversation" disabled>
                      <CircleStopIcon className="size-3.5" />
                      Stop session
                    </DropdownMenuItem>
                  </div>
                </TooltipTrigger>
                <TooltipContent side="left">
                  Only the session owner can stop this session
                </TooltipContent>
              </Tooltip>
            ))}
          {isOwner ? (
            <DropdownMenuItem
              data-testid="delete-conversation"
              variant="destructive"
              onSelect={onDelete}
            >
              <Trash2Icon className="size-3.5" />
              Delete
            </DropdownMenuItem>
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                <div>
                  <DropdownMenuItem data-testid="delete-conversation" disabled>
                    <Trash2Icon className="size-3.5" />
                    Delete
                  </DropdownMenuItem>
                </div>
              </TooltipTrigger>
              <TooltipContent side="left">
                Only the session owner can delete this session
              </TooltipContent>
            </Tooltip>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}