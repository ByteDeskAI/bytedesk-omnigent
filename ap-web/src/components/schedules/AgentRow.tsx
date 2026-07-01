import { Badge } from "@/components/ui/badge";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { tierForAgent, TIER_LABELS } from "@/lib/agentTiers";

export function AgentRow({
  agent,
  selected,
  onSelect,
}: {
  agent: AvailableAgent;
  selected: boolean;
  onSelect: () => void;
}) {
  // Employees carry no badge (matching the prior bare-workflow rule); managed
  // and workflow tiers get a tier label.
  const tier = tierForAgent(agent);
  return (
    <button
      type="button"
      onClick={onSelect}
      className={[
        "mb-1 flex min-h-14 w-full cursor-pointer items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors",
        selected
          ? "border-primary/50 bg-primary/10 text-foreground"
          : "border-transparent text-muted-foreground hover:border-border hover:bg-muted/60 hover:text-foreground",
      ].join(" ")}
    >
      <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-background text-xs font-medium">
        {agent.display_name.slice(0, 2).toUpperCase()}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{agent.display_name}</span>
        <span className="block truncate text-xs">{agent.title ?? agent.name}</span>
      </span>
      {tier !== "employee" && <Badge variant="secondary">{TIER_LABELS[tier]}</Badge>}
    </button>
  );
}