import { BotIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { CHAT_COLUMN_WIDTH } from "./chat-utils";

/**
 * Peeking tray tucked behind the composer's top edge while the active
 * session is a sub-agent (child).
 */
export function SubagentComposerTray({ label }: { label: string }) {
  return (
    <div
      data-testid="composer-subagent-tray"
      className={cn(
        "mx-auto -mb-4 flex w-full items-center gap-1.5 rounded-t-2xl bg-brand-accent/10 px-4 pt-1.5 pb-5.5 text-xs text-brand-accent",
        CHAT_COLUMN_WIDTH,
      )}
    >
      <BotIcon className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 truncate">
        Chatting with sub-agent <strong className="font-semibold">{label}</strong>
      </span>
    </div>
  );
}