import { Loader2Icon } from "lucide-react";
import {
  ConversationEmptyState,
} from "@/components/ai-elements/conversation";
import { Message, MessageContent } from "@/components/ai-elements/message";
import { useChatStore } from "@/store/chatStore";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { SANDBOX_STAGE_LABELS } from "./ConnectionIndicator";

/**
 * Main-pane launch indicator for "session is coming up" states.
 */
export function RunnerStartingIndicator({ variant }: { variant: "hero" | "row" }) {
  const terminalFirst = useTerminalFirst();
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  const sandboxLabel =
    sandboxStatus !== null && sandboxStatus.stage !== "failed"
      ? SANDBOX_STAGE_LABELS[sandboxStatus.stage]
      : undefined;
  const terminalSpinUp = Boolean(
    terminalFirst?.isTerminalFirst && terminalFirst.terminalStartingUp,
  );
  if (sandboxLabel === undefined && !terminalSpinUp) {
    return null;
  }
  const line =
    sandboxLabel !== undefined ? `${sandboxLabel}…` : "Starting up… getting your terminal ready.";
  if (variant === "hero") {
    return (
      <ConversationEmptyState
        data-testid="runner-starting-indicator"
        role="status"
        aria-live="polite"
        icon={<Loader2Icon className="size-7 animate-spin" aria-hidden />}
        title={sandboxLabel !== undefined ? `${sandboxLabel}…` : "Starting up…"}
        description={
          sandboxLabel !== undefined
            ? "Setting up your sandbox — this can take a minute."
            : "Getting your terminal ready — this can take a few seconds."
        }
      />
    );
  }
  return (
    <Message
      from="assistant"
      data-testid="runner-starting-indicator"
      role="status"
      aria-live="polite"
    >
      <MessageContent>
        <span className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2Icon className="size-4 shrink-0 animate-spin" aria-hidden />
          {line}
        </span>
      </MessageContent>
    </Message>
  );
}