import { LockIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { AgentRowTooltip } from "@/components/AgentHoverCard";
import { writeLastAgentId } from "@/lib/agentPreferences";
import { tierForAgent } from "@/lib/agentTiers";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { AGENT_PICKER_DESCRIPTIONS } from "./newChatLandingConstants";
import { harnessUnconfiguredOnHost } from "./newChatLandingUtils";

export function NewChatAgentPickerRow({
  agent,
  effectiveAgentId,
  harnessWarningHost,
  onPickHarnessClear,
  setPickedAgentId,
}: {
  agent: AvailableAgent;
  effectiveAgentId: string | null;
  harnessWarningHost: Host | undefined;
  onPickHarnessClear: () => void;
  setPickedAgentId: (id: string) => void;
}) {
  const blurb = AGENT_PICKER_DESCRIPTIONS[agent.name];

  return (
    <DropdownMenuItem
      key={agent.id}
      data-testid={`new-chat-landing-agent-${agent.id}`}
      data-active={agent.id === effectiveAgentId ? "true" : undefined}
      onSelect={() => {
        // Switching agents drops the harness override so a

        // pick never leaks across agents.

        if (agent.id !== effectiveAgentId) onPickHarnessClear();

        setPickedAgentId(agent.id);

        // Explicit picks persist; auto-defaults never do.

        writeLastAgentId(agent.id);
      }}
      className="items-start gap-2 rounded-sm px-2 py-1.5 text-sm data-[active=true]:bg-accent/60 data-[active=true]:text-foreground"
    >
      {/* Cursor-style flyout to the right of the row. The tooltip wraps

            the row's inner content (a host <div>), NOT the menu item:

            DropdownMenuItem is a plain function component (no forwardRef),

            so TooltipTrigger's `asChild` ref can't attach to it under

            React 18 — the flyout wouldn't open and it logs ref warnings.

            Wrapping the <div> keeps refs working and the item a direct

            roving-focus child of DropdownMenuContent. No-ops when the

            agent has no description. */}

      <AgentRowTooltip agent={agent}>
        <div className="flex min-w-0 flex-1 items-baseline gap-2.5">
          <span className="truncate">{agent.display_name}</span>

          {/* Managed tiers are platform-authored: a small lock

                marks them; there is no per-agent edit/delete UI to suppress. */}

          {["system", "harness"].includes(tierForAgent(agent)) && (
            <LockIcon
              className="size-3 shrink-0 self-center text-muted-foreground"
              data-testid={`new-chat-landing-agent-lock-${agent.id}`}
              aria-label="Managed agent (read-only)"
            />
          )}

          {blurb && <span className="truncate text-[11px] text-muted-foreground/70">{blurb}</span>}
        </div>
      </AgentRowTooltip>

      {/* Compact right-aligned readiness pill; the full

            remediation text lives in the composer warning. */}

      {harnessUnconfiguredOnHost(agent.harness, harnessWarningHost) && (
        <Badge
          variant="outline"
          className="ml-auto self-center border-amber-300 bg-amber-50 text-[11px] text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400"
          data-testid={`new-chat-landing-agent-warning-${agent.id}`}
        >
          needs setup
        </Badge>
      )}
    </DropdownMenuItem>
  );
}
