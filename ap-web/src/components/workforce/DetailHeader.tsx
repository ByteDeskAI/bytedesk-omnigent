import { RefreshCwIcon } from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { AgentTier } from "@/lib/agentTiers";
import { cn } from "@/lib/utils";
import { Metric } from "./Metric";
import {
  agentDisplayName,
  iconForTier,
  tierAccentRingClass,
  tierAccentTextClass,
  tierInitials,
  tierLabel,
} from "./workforce-utils";

export function DetailHeader({
  agent,
  tier,
  editable,
  refetch,
}: {
  agent: AvailableAgent;
  tier: AgentTier;
  editable: boolean;
  refetch: () => void;
}) {
  return (
    <header className="mc-fade-up shrink-0 border-b border-border-dimmer bg-bg-subtle px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <Avatar size="lg" className={cn("ring-2 shrink-0", tierAccentRingClass(tier))}>
            <AvatarFallback className={cn("bg-background", tierAccentTextClass(tier))}>
              {tier === "employee" ? tierInitials(agent) : iconForTier(tier)}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate text-xl font-semibold">{agentDisplayName(agent)}</h2>
              <Badge variant={editable ? "secondary" : "outline"} className="gap-1.5">
                {editable && (
                  <span
                    className="size-1.5 rounded-full bg-accent-green mc-live-dot"
                    aria-hidden="true"
                  />
                )}
                {editable ? "Editable" : "Read-only"}
              </Badge>
              <Badge variant="outline" className={tierAccentTextClass(tier)}>
                {tierLabel(tier)}
              </Badge>
            </div>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              {agent.description || agent.title || agent.name}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Metric label="id" value={agent.id} />
              <Metric label="harness" value={agent.harness || "unknown"} />
              <Metric label="department" value={agent.department || "Unassigned"} />
            </div>
          </div>
        </div>
        <Button variant="ghost" size="icon" onClick={refetch} aria-label="Refresh agent">
          <RefreshCwIcon />
        </Button>
      </div>
    </header>
  );
}