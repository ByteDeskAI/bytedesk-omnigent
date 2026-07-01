import { Loader2Icon, MessageSquareIcon, TerminalIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { CHAT_COLUMN_WIDTH } from "./chat-utils";

/**
 * Chat/Terminal segmented control for terminal-first sessions.
 */
export function ConnectedTerminalFirstPill({
  ctx,
}: {
  ctx: NonNullable<ReturnType<typeof useTerminalFirst>>;
}) {
  const { view, setView, terminalsAvailable, terminalStartingUp } = ctx;
  return (
    <div
      className={cn(
        "mx-auto flex w-full items-center justify-center px-6 pb-1.5",
        CHAT_COLUMN_WIDTH,
      )}
    >
      <div
        role="group"
        aria-label="View mode"
        className="flex items-center gap-1 rounded-full border border-border bg-card/90 p-1 text-xs shadow-sm"
      >
        <div className="flex items-center gap-0.5">
          <button
            type="button"
            aria-pressed={view === "chat"}
            aria-label="Chat"
            onClick={() => setView("chat")}
            className={cn(
              "flex cursor-pointer items-center gap-1 rounded-full px-2 py-0.5 transition-colors",
              view === "chat"
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
            )}
          >
            <MessageSquareIcon className="size-3.5 shrink-0" />
            <span>Chat</span>
          </button>
          <button
            type="button"
            aria-pressed={view === "terminal"}
            aria-label="Terminal"
            disabled={!terminalsAvailable}
            title={terminalStartingUp ? "Terminal is starting up…" : undefined}
            onClick={() => setView("terminal")}
            className={cn(
              "flex cursor-pointer items-center gap-1 rounded-full px-2 py-0.5 transition-colors disabled:cursor-not-allowed disabled:opacity-50",
              view === "terminal"
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
            )}
          >
            {terminalStartingUp ? (
              <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
            ) : (
              <TerminalIcon className="size-3.5 shrink-0" />
            )}
            <span>Terminal</span>
          </button>
        </div>
      </div>
    </div>
  );
}