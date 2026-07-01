import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { tierForAgent } from "@/lib/agentTiers";
import { cn } from "@/lib/utils";
import {
  agentDisplayName,
  iconForTier,
  tierAccentBorderClass,
  tierAccentRingClass,
  tierAccentTextClass,
  tierInitials,
} from "./workforce-utils";

export function RosterButton({
  agent,
  selected,
  onSelect,
}: {
  agent: AvailableAgent;
  selected: boolean;
  onSelect: () => void;
}) {
  const tier = tierForAgent(agent);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "group/roster flex min-h-14 w-full items-center gap-2.5 rounded-md border px-2.5 py-2 text-left transition-all duration-150",
        selected
          ? cn("bg-muted text-foreground", tierAccentBorderClass(tier))
          : "border-transparent text-muted-foreground hover:translate-x-0.5 hover:bg-muted/50 hover:text-foreground",
      )}
    >
      <Avatar
        size="sm"
        className={cn(
          "ring-2 transition-all",
          selected ? tierAccentRingClass(tier) : "ring-transparent group-hover/roster:ring-border",
        )}
      >
        <AvatarFallback className={cn("bg-background", tierAccentTextClass(tier))}>
          {tier === "employee" ? tierInitials(agent) : iconForTier(tier)}
        </AvatarFallback>
      </Avatar>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{agentDisplayName(agent)}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {agent.title || agent.department || agent.name}
        </span>
      </span>
      {tier === "workflow" && <Badge variant="outline">Read-only</Badge>}
    </button>
  );
}