import { Badge } from "@/components/ui/badge";
import { RunningDot } from "@/components/RunningDot";
import { cn } from "@/lib/utils";
import { DOT_TONE, QUIET_STATE, type AgentStatus } from "./subagentsPanelUtils";

export function SubagentStatusIndicator({ activity, label, details }: AgentStatus) {
  const title = details ? `${label}: ${details}` : label;
  if (activity === "awaiting") {
    return (
      <span
        aria-label={title}
        title={title}
        data-testid="subagent-status-dot"
        className="inline-flex shrink-0 items-center text-xs"
      >
        <Badge className="border-transparent bg-warning/15 text-warning">Needs response</Badge>
      </span>
    );
  }
  if (activity === "failed") {
    return (
      <span
        aria-label={title}
        title={title}
        data-testid="subagent-status-dot"
        className="inline-flex shrink-0 items-center gap-1 text-destructive text-xs"
      >
        <span>{label}</span>
        <span className={cn("inline-block size-2 shrink-0 rounded-full", DOT_TONE.failed)} />
      </span>
    );
  }
  return (
    <span
      aria-label={title}
      title={title}
      data-testid="subagent-status-dot"
      className="inline-flex shrink-0 items-center gap-1 text-muted-foreground text-xs"
    >
      {!QUIET_STATE[activity] && <span>{label}</span>}
      {activity === "working" ? (
        <RunningDot />
      ) : (
        <span className={cn("inline-block size-2 shrink-0 rounded-full", DOT_TONE[activity])} />
      )}
    </span>
  );
}