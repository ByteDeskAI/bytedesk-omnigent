import { useRef, useState } from "react";
import { CheckIcon, CopyIcon, GitForkIcon, Share2Icon, XIcon } from "lucide-react";
import {
  Message,
  MessageActions,
  MessageAction,
  MessageContent,
} from "@/components/ai-elements/message";
import { BlockRenderer } from "@/components/blocks/BlockRenderer";
import { shareMessageContent } from "@/lib/pwa/shareMessage";
import type { Bubble } from "@/lib/renderItems";
import { useChatStore } from "@/store/chatStore";
import { useForkDialog } from "@/shell/ForkDialogContext";
import { collectBubbleMarkdown, containsMarkdownTable } from "./chat-utils";

export function AssistantBubble({ bubble }: { bubble: Extract<Bubble, { kind: "assistant" }> }) {
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const conversationId = useChatStore((s) => s.conversationId);
  const [isCopied, setIsCopied] = useState(false);
  const copyTimeoutRef = useRef<number>(0);
  const forkDialog = useForkDialog();

  if (bubble.items.length === 0) return null;

  const markdownText = collectBubbleMarkdown(bubble.items);
  const hasElicitation = bubble.items.some((it) => it.kind === "elicitation");
  const isWide = hasElicitation || containsMarkdownTable(bubble.items);

  const handleCopy = async () => {
    if (!markdownText || !navigator?.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(markdownText);
      setIsCopied(true);
      window.clearTimeout(copyTimeoutRef.current);
      copyTimeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
    } catch {
      // ignore clipboard errors
    }
  };

  const handleShare = async () => {
    if (!markdownText) return;
    const convId = useChatStore.getState().conversationId;
    const url =
      typeof window !== "undefined" && convId
        ? `${window.location.origin}/c/${convId}`
        : typeof window !== "undefined"
          ? window.location.href
          : "";
    await shareMessageContent({
      title: "Omnigent conversation",
      text: markdownText.slice(0, 500),
      url,
    });
  };

  return (
    <>
      <Message
        from="assistant"
        data-testid="message-bubble"
        data-role="assistant"
        className={isWide ? "max-w-full" : "max-w-3xl"}
      >
        <MessageContent className={isWide ? "w-full" : undefined}>
          <BlockRenderer
            items={bubble.items}
            sessionStatus={sessionStatus}
            conversationId={conversationId}
          />
        </MessageContent>
        {bubble.lifecycle === "cancelled" && (
          <p
            className="mt-1 flex items-center gap-1 text-xs text-muted-foreground"
            data-testid="assistant-interrupted-indicator"
          >
            <XIcon className="size-3" aria-hidden="true" />
            <span>Interrupted</span>
          </p>
        )}
        {markdownText && (
          <MessageActions className="mt-1 opacity-40 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
            <MessageAction tooltip="Copy" onClick={handleCopy}>
              {isCopied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
            </MessageAction>
            <MessageAction tooltip="Share" onClick={() => void handleShare()}>
              <Share2Icon size={14} />
            </MessageAction>
            {forkDialog?.canFork && bubble.lifecycle !== "streaming" && (
              <MessageAction
                tooltip="Fork from here"
                data-testid="fork-from-response"
                onClick={() => forkDialog.openForkDialog({ upToResponseId: bubble.responseId })}
              >
                <GitForkIcon size={14} />
              </MessageAction>
            )}
          </MessageActions>
        )}
      </Message>

      {bubble.lifecycle === "failed" && (
        <p className="text-destructive text-xs">Error: {bubble.error}</p>
      )}
    </>
  );
}