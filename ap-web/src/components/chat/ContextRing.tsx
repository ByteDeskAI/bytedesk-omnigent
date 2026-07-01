import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/** Circumference of the progress ring (r=5.5). */
const RING_CIRCUMFERENCE = 2 * Math.PI * 5.5;

/** Circular progress ring showing how much context window is used. */
export function ContextRing({ contextWindow, tokensUsed }: { contextWindow: number; tokensUsed: number }) {
  const pct = Math.min(tokensUsed / contextWindow, 1);
  const usedArc = pct * RING_CIRCUMFERENCE;
  const usedPct = Math.round(pct * 100);

  const color =
    pct > 0.8 ? "text-destructive" : pct > 0.6 ? "text-warning" : "text-muted-foreground";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn("flex items-center gap-1.5", color)}
          aria-label={`${usedPct}% of context used`}
        >
          <svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true">
            <circle cx="8" cy="8" r="5.5" stroke="currentColor" strokeWidth="2" opacity="0.2" />
            {usedArc > 0 && (
              <circle
                cx="8"
                cy="8"
                r="5.5"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeDasharray={`${usedArc} ${RING_CIRCUMFERENCE}`}
                transform="rotate(-90 8 8)"
              />
            )}
          </svg>
          <span className="text-xs tabular-nums" aria-hidden="true">
            {usedPct}%
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-44 text-center text-xs">
        <p className="tabular-nums">{usedPct}% of context used.</p>
      </TooltipContent>
    </Tooltip>
  );
}