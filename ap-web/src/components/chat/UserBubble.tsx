import { useContext } from "react";
import { FileTextIcon, ImageIcon } from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Message, MessageContent } from "@/components/ai-elements/message";
import { FilePathAwareMessageResponse } from "@/components/blocks/BlockRenderer";
import { SystemMessageView } from "@/components/blocks/SystemMessage";
import { parseSystemMessage } from "@/lib/systemMessage";
import { userColor, userColorTint, userInitials } from "@/lib/userBadge";
import { getCurrentAuthorId } from "@/lib/identity";
import { cn } from "@/lib/utils";
import type { MessageContentBlock } from "@/lib/blocks";
import type { Bubble } from "@/lib/renderItems";
import { useChatStore } from "@/store/chatStore";
import { SessionImage } from "@/components/SessionImage";
import { SessionSharedContext } from "./SessionSharedContext";
import { extractUserText, shouldShowAuthorBadge } from "./chat-utils";

export function UserBubble({ bubble }: { bubble: Extract<Bubble, { kind: "user" }> }) {
  const sessionId = useChatStore((s) => s.conversationId);
  const isSessionShared = useContext(SessionSharedContext);
  const text = extractUserText(bubble.content);
  const images = bubble.content.filter(
    (c): c is Extract<MessageContentBlock, { type: "input_image" }> => c.type === "input_image",
  );
  const fileChips = bubble.content.filter(
    (c): c is Extract<MessageContentBlock, { type: "input_file" }> => c.type === "input_file",
  );
  if (images.length === 0 && fileChips.length === 0) {
    const parsed = parseSystemMessage(text);
    if (parsed) return <SystemMessageView message={parsed} />;
  }
  const author = bubble.createdBy;
  const showAuthorBadge = shouldShowAuthorBadge(author, getCurrentAuthorId(), isSessionShared);
  const flashing = useChatStore((s) => s.flashItemId === bubble.itemId);
  return (
    <Message
      from="user"
      data-testid="message-bubble"
      data-role="user"
      data-user-message-id={bubble.itemId}
      className="max-w-3xl"
    >
      <div className="ml-auto flex w-fit max-w-full items-center gap-1.5">
        {showAuthorBadge && author && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Avatar
                size="sm"
                data-testid="message-author"
                aria-label={author}
                className="shrink-0"
              >
                <AvatarFallback
                  className="font-medium text-white"
                  style={{ backgroundColor: userColor(author) }}
                >
                  {userInitials(author)}
                </AvatarFallback>
              </Avatar>
            </TooltipTrigger>
            <TooltipContent>{author}</TooltipContent>
          </Tooltip>
        )}
        <MessageContent
          className={cn(flashing && "animate-user-msg-flash")}
          style={showAuthorBadge && author ? { backgroundColor: userColorTint(author) } : undefined}
        >
          {images.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-2">
              {images.map((img, i) =>
                img.file_id.startsWith("pending:") ? (
                  <span
                    key={i}
                    className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                  >
                    <ImageIcon className="size-3 shrink-0" />
                    <span className="max-w-[180px] truncate">
                      {img.filename ?? img.file_id.replace("pending:", "")}
                    </span>
                  </span>
                ) : (
                  <SessionImage
                    key={i}
                    path={
                      sessionId
                        ? `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(img.file_id)}/content`
                        : undefined
                    }
                    alt={img.filename ?? img.file_id}
                    className="max-h-64 max-w-full rounded-md object-contain"
                  />
                ),
              )}
            </div>
          )}
          {fileChips.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-1.5">
              {fileChips.map((att, i) => (
                <span
                  key={i}
                  className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                >
                  <FileTextIcon className="size-3 shrink-0" />
                  <span className="max-w-[180px] truncate">{att.filename ?? att.file_id}</span>
                </span>
              ))}
            </div>
          )}
          {text && <FilePathAwareMessageResponse breaks>{text}</FilePathAwareMessageResponse>}
        </MessageContent>
      </div>
    </Message>
  );
}