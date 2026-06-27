import { GaugeIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  ActivityFeedPanel,
  AutonomyStrip,
  CommanderChat,
  DecisionReplayPanel,
  RoiFrontierPanel,
  TreasuryPanel,
  useCommanderSession,
} from "@/components/goal-command";
import { useGoalEvents } from "@/hooks/useGoals";
import { Link } from "@/lib/routing";

/**
 * Goals Command Center (BDP-2598) — the founder's surface to SET, MONITOR,
 * and OBSERVE the autonomous goal engine. The left column is the
 * conversational goal-commander (set/drive); the center + right columns are
 * the live cockpit (frontier, activity, treasury, decisions, autonomy).
 * Mutations stay conversational; the cockpit is read-only + SSE-live.
 */
export function CommandCenterPage() {
  const commander = useCommanderSession();
  // One SSE subscription for the whole cockpit: invalidates the goal /
  // frontier / decision / outcome queries so every panel stays live.
  useGoalEvents(true);

  return (
    <div className="fixed inset-3 z-50 flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-background shadow-2xl">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted">
            <GaugeIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold">Goals Command Center</h1>
            <p className="truncate text-xs text-muted-foreground">
              Set, monitor, and observe the autonomous org
            </p>
          </div>
        </div>
        <Button variant="ghost" size="icon" asChild aria-label="Close command center">
          <Link to="/">
            <XIcon />
          </Link>
        </Button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_22rem_22rem] xl:grid-cols-[minmax(0,1.2fr)_24rem_24rem]">
        <section className="min-h-0 overflow-hidden border-b border-border lg:border-r lg:border-b-0">
          <CommanderChat
            agentId={commander.agentId}
            sessionId={commander.sessionId}
            starting={commander.starting}
            error={commander.error}
          />
        </section>

        <section className="grid min-h-0 grid-rows-[minmax(0,1.4fr)_minmax(0,1fr)] gap-3 overflow-hidden border-b border-border p-3 lg:border-r lg:border-b-0">
          <RoiFrontierPanel />
          <ActivityFeedPanel />
        </section>

        <section className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)_minmax(0,1fr)] gap-3 overflow-hidden p-3">
          <AutonomyStrip agentId={commander.agentId} sessionId={commander.sessionId} />
          <TreasuryPanel />
          <DecisionReplayPanel />
        </section>
      </div>
    </div>
  );
}
