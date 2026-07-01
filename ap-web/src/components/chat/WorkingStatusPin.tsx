import { useStickToBottomContext } from "use-stick-to-bottom";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { AgentMascotEyes } from "@/components/AgentMascotEyes";
import { cn } from "@/lib/utils";
import { CHAT_COLUMN_WIDTH } from "./chat-utils";

/**
 * Scroll-pinned "Working…" pill — sole aria-live region (inline shimmer is
 * aria-hidden).
 */
export function WorkingStatusPin({ show, suppress = false }: { show: boolean; suppress?: boolean }) {
  const { isAtBottom } = useStickToBottomContext();
  const visible = show && !isAtBottom && !suppress;
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="working-indicator-pin"
      className={cn(
        "pointer-events-none absolute inset-x-0 bottom-0 z-20 transition-opacity duration-200",
        visible ? "opacity-100" : "opacity-0",
      )}
    >
      <div className={cn("mx-auto w-full px-6", CHAT_COLUMN_WIDTH)}>
        {show && (
          <div
            className={cn(
              "flex w-fit items-center gap-1.5 rounded-t-lg border border-b-0 border-border bg-card px-3 pt-1 pb-1.5",
              !visible && "sr-only",
            )}
          >
            <AgentMascotEyes decorative className="otto-working h-4 w-auto shrink-0" />
            <Shimmer className="text-xs font-mono" duration={1.5}>
              Working…
            </Shimmer>
          </div>
        )}
      </div>
    </div>
  );
}